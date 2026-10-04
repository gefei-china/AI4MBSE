# -*- coding: utf-8 -*-
"""P0-2（孤儿编排任务回收）回归验证 —— 零 LLM、独立临时库，可反复跑。

**为什么需要它**（《架构-可扩展性-稳定性整体评估》§4.2 / §6 P0-2 实测）：
    `agent_tasks` 共 60 个批次，其中 **16 个批次残留 planned/ready/blocked**，最老的
    4.3 天没有任何推进 —— 既没被判失败，也没有任何界面能看到它们卡着。用户侧表现：
    编排流水线一直"执行中"，重跑也不行。对标 LangGraph（checkpointer + resume）与
    Temporal（timeout/自动重投），它们的**共同前提**都是"先能识别死掉的运行"。
    本整改做第一步：liveness 判据 + 显式失败化 + 可观测。（**不做**自动 resume ——
    那需要独立 worker 队列，属 P0-1/P0-2 第二步，本报告 §7.2 已列为依赖顺序。）

本脚本锁定的不变式：
    I1 超过 TTL 未推进的非终态任务 ⇒ 必须被回收为 failed，且写明孤儿原因
    I2 **反向（最关键的防误杀）**：TTL 内、或状态处于终态的任务 ⇒ **绝不能**被回收
    I3 回收**保住上下文**：context / result / config 原样保留（否则没法重跑、也没证据）
    I4 dry_run=True ⇒ 只报告不改写（上线灰度止损点）
    I5 时间口径必须与 `CURRENT_TIMESTAMP` 一致（UTC）：误用 localtime 会让阈值偏移整时区
    I6 表/列缺失时不抛异常（启动路径不得被回收逻辑带崩）
    I7 summary 计数与实际一致，含批次去重

**变异自证（强制）**：把"修复前的写法"还原，脚本必须判 FAIL/VACUOUS。
   工程纪律（skill §6.2）：能抓住旧写法才算数。本脚本内置 3 组变异。

用法（**裸跑自身即完整口径**）：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_orphan_reap.py
退出码：全绿 0 / 有失败或空转 1。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

RR_SRC = os.path.join(ROOT, "core", "run_registry.py")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((kind if not ok else PASS, name, detail))
    print(f"  {PASS if ok else kind}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


DDL = """CREATE TABLE agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER DEFAULT 0, task_key TEXT DEFAULT '', title TEXT DEFAULT '',
    agent_id TEXT DEFAULT '', task_type TEXT DEFAULT 'agent',
    config TEXT DEFAULT '{}', deps TEXT DEFAULT '[]', status TEXT DEFAULT 'planned',
    result TEXT DEFAULT '', metadata TEXT DEFAULT '{}', error TEXT DEFAULT '',
    assigned_by TEXT DEFAULT 'planner', seq INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    context TEXT DEFAULT '', expected_output TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0)"""


def _db():
    """独立内存库 + 真实命名口径（`updated_at` 默认 CURRENT_TIMESTAMP = UTC）。"""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(DDL)
    con.commit()
    return con


def _seed(con, run, status, age_s, task_key="t1", conv=1, context='{"step":3}',
          result="已完成 3/5 步", updated=None):
    import datetime
    if updated is None:
        updated = (datetime.datetime.utcnow() - datetime.timedelta(seconds=age_s)).strftime("%Y-%m-%d %H:%M:%S")
    cur = con.execute(
        "INSERT INTO agent_tasks (run_id, conversation_id, task_key, title, status, context, result,"
        " config, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (run, conv, task_key, "task " + task_key, status, context, result, '{"q":"x"}', updated))
    con.commit()
    return cur.lastrowid


def _load_rr(mutate=None, cfg_ttl=600):
    """就地加载 core/run_registry.py（可选源码级变异）。

    为什么要 exec 源码文本而非 `import`：`import` 拿到的是已缓存字节码，无法注入变异；
    而在测试里"手写一份等价实现"会与被测代码脱钩 —— 那正是 2026-09-30 让变异测试
    整体假绿的根因，不能再犯。
    """
    src = open(RR_SRC, encoding="utf-8").read()
    if mutate:
        old, new, anchor = mutate
        assert anchor in src, f"变异锚点丢失：{anchor}"
        src = src.replace(old, new, 1)
    ns = {"__name__": "core.run_registry"}
    exec(compile(src, "core/run_registry.py", "exec"), ns)
    return ns


# ══════════════════ I1：孤儿必须被回收 ══════════════════
def t_i1():
    print("\n=== I1 超时未推进 ⇒ 必须失败化（不是原地挂着）===")
    rr = _load_rr()
    con = _db()
    i_old = _seed(con, run=101, status="running", age_s=9000)   # >> TTL 600s
    res = rr["reap_orphans"](con, ttl_s=600, audit=False)
    row = con.execute("SELECT status, error FROM agent_tasks WHERE id=?", (i_old,)).fetchone()
    ok = _rec("I1a 回收计数正确", res["reaped"] == 1 and res["scanned"] == 1, f"got={res}")
    ok &= _rec("I1b 状态改为 failed", row["status"] == "failed", f"got={row['status']}")
    ok &= _rec("I1c 写明孤儿原因（可排障）",
               ("孤儿" in (row["error"] or "")) and ("600" in row["error"] or "10" in row["error"]),
               f"error={row['error']!r}")
    ok &= _rec("I1d 返回受影响 run_id", res["run_ids"] == [101], f"got={res['run_ids']}")
    return ok


# ══════════════════ I2：不得误杀在跑的任务（最关键反向断言）══════════════════
def t_i2():
    print("\n=== I2 反向：TTL 内 / 终态 ⇒ 绝不能被回收 ===")
    rr = _load_rr()
    con = _db()
    i_fresh = _seed(con, run=201, status="running", age_s=30)      # TTL 600 内的活跃任务
    i_done = _seed(con, run=202, status="done", age_s=99999)       # 终态，再老也不动
    i_fail = _seed(con, run=203, status="failed", age_s=99999)
    i_cancel = _seed(con, run=204, status="canceled", age_s=99999)
    res = rr["reap_orphans"](con, ttl_s=600, audit=False)
    st = {r["id"]: r["status"] for r in
          con.execute("SELECT id, status FROM agent_tasks").fetchall()}
    ok = _rec("I2a 一条都没被回收", res["reaped"] == 0, f"got={res}")
    ok &= _rec("I2b TTL 内的 running 保持 running（**不得误杀**）",
               st[i_fresh] == "running", f"got={st[i_fresh]}")
    ok &= _rec("I2c 终态 done/failed/canceled 不被改写",
               (st[i_done], st[i_fail], st[i_cancel]) == ("done", "failed", "canceled"),
               f"got={st}")
    # 阈值边界：正好等于 TTL 也算未超时（避免边界抖动把刚好的任务判死）
    con2 = _db()
    i_edge = _seed(con2, run=205, status="running", age_s=600)
    con2.execute("UPDATE agent_tasks SET updated_at = datetime('now','-601 seconds') WHERE id=?",
                 (i_edge,))
    con2.commit()
    r2 = rr["reap_orphans"](con2, ttl_s=600, audit=False)
    s2 = con2.execute("SELECT status FROM agent_tasks WHERE id=?", (i_edge,)).fetchone()["status"]
    ok &= _rec("I2d 超过 TTL 1 秒即回收（边界另一侧有效）",
               r2["reaped"] == 1 and s2 == "failed", f"reaped={r2['reaped']} status={s2}")
    return ok


def m_i2():
    """变异：把非终态集合写成含终态 → 连 done 的任务也会被回收（严重误伤）。"""
    print("\n=== 变异 M2：判据扩大到全部状态（旧写法方向：连 done 都收）===")
    rr = _load_rr(mutate=('ACTIVE_STATUSES = ("planned", "ready", "running", "blocked")',
                          'ACTIVE_STATUSES = ("planned", "ready", "running", "blocked", "done", "failed")',
                          "ACTIVE_STATUSES = ("))
    con = _db()
    i_done = _seed(con, run=301, status="done", age_s=99999)
    rr["reap_orphans"](con, ttl_s=600, audit=False)
    s = con.execute("SELECT status FROM agent_tasks WHERE id=?", (i_done,)).fetchone()["status"]
    return _rec("M2 旧写法复现（终态被误伤）→ 证明 I2c 非空转", s == "failed", "", VACUOUS)


def m_i2b():
    """变异：时间判据反向（`<` → `>`）⇒ 会回收**最新**的任务，放着老孤儿不管。"""
    print("\n=== 变异 M2b：时间判据反向 ===")
    rr = _load_rr(mutate=("AND updated_at < datetime('now', ?) ORDER BY updated_at ASC",
                          "AND updated_at > datetime('now', ?) ORDER BY updated_at ASC",
                          "updated_at < datetime"))
    con = _db()
    i_old = _seed(con, run=401, status="running", age_s=99999)
    i_fresh = _seed(con, run=402, status="running", age_s=5)
    res = rr["reap_orphans"](con, ttl_s=600, audit=False)
    st = {r["id"]: r["status"] for r in con.execute("SELECT id,status FROM agent_tasks").fetchall()}
    caught = (st[i_fresh] == "failed" and st[i_old] == "running")
    return _rec("M2b 旧写法复现（误杀新任务、放过老孤儿）→ 证明 I2b 非空转",
                caught and res["reaped"] >= 1, "", VACUOUS)


# ══════════════════ I3：保住上下文 ══════════════════
def t_i3():
    print("\n=== I3 回收必须留住上下文（重跑与取证都靠它）===")
    rr = _load_rr()
    con = _db()
    i = _seed(con, run=501, status="blocked", age_s=7000, context='{"node":"t3","artifact":"id-9"}',
              result="子任务 t3 已产出草稿")
    rr["reap_orphans"](con, ttl_s=600, audit=False)
    row = con.execute("SELECT status, context, result, config FROM agent_tasks WHERE id=?", (i,)).fetchone()
    ok = _rec("I3a context 未被清空", row["context"] == '{"node":"t3","artifact":"id-9"}', row["context"])
    ok &= _rec("I3b result 未被清空", "草稿" in (row["result"] or ""), row["result"])
    ok &= _rec("I3c config 未被清空", row["config"] == '{"q":"x"}', row["config"])
    return ok


# ══════════════════ I4：dry_run ══════════════════
def t_i4():
    print("\n=== I4 dry_run：只报告不改写（上线灰度止损）===")
    rr = _load_rr()
    con = _db()
    i = _seed(con, run=601, status="planned", age_s=9000)
    res = rr["reap_orphans"](con, ttl_s=600, dry_run=True, audit=False)
    st = con.execute("SELECT status FROM agent_tasks WHERE id=?", (i,)).fetchone()["status"]
    ok = _rec("I4a dry_run 不改状态", st == "planned", f"got={st}")
    ok &= _rec("I4b dry_run 仍给出完整清单（人机核对用）",
               res["reaped"] == 0 and res["task_ids"] == [i] and res["dry_run"] is True, f"got={res}")
    # 二次：关掉 dry_run 后同一批必须真被收（"看过了就能收"）
    rr["reap_orphans"](con, ttl_s=600, dry_run=False, audit=False)
    st2 = con.execute("SELECT status FROM agent_tasks WHERE id=?", (i,)).fetchone()["status"]
    ok &= _rec("I4c 关掉 dry_run 后确实收掉", st2 == "failed", f"got={st2}")
    return ok


# ══════════════════ I5：UTC 口径 ══════════════════
def t_i5():
    print("\n=== I5 时间口径必须与 CURRENT_TIMESTAMP(UTC) 一致 ===")
    src = open(RR_SRC, encoding="utf-8").read()
    body = src[src.find("def find_orphans"):]
    ok = _rec("I5a 查询未使用 'localtime' 修饰符", "'localtime'" not in body)
    ok &= _rec("I5b 与 agent_tasks 默认值同为 UTC 口径（datetime('now')）",
               "datetime('now', ?)" in body)
    # 运行时交叉验证：用 SQLite 自己的 CURRENT_TIMESTAMP 造行，再交给回收判断
    rr = _load_rr()
    con = _db()
    con.execute("INSERT INTO agent_tasks (run_id, task_key, status) VALUES (701,'t1','running')")
    con.commit()                                     # updated_at = CURRENT_TIMESTAMP（UTC，此刻）
    now_res = rr["find_orphans"](con, ttl_s=600)
    ok &= _rec("I5c 刚写入的活任务不判为孤儿（口径一致的实证）", now_res == [], f"got={len(now_res)}")
    con.execute("UPDATE agent_tasks SET updated_at=datetime('now','-700 seconds')")
    con.commit()
    ok &= _rec("I5d 回拨 700s 后判为孤儿", len(rr["find_orphans"](con, ttl_s=600)) == 1)
    return ok


# ══════════════════ I6：容错 ══════════════════
def t_i6():
    print("\n=== I6 表缺失/脏数据不得把启动路径带崩 ===")
    rr = _load_rr()
    con = sqlite3.connect(":memory:")     # 空库，连 agent_tasks 都没有
    con.row_factory = sqlite3.Row
    try:
        ok = _rec("I6a find_orphans 在空库返回 []", rr["find_orphans"](con, ttl_s=600) == [])
        ok &= _rec("I6b summary 在空库返回 0 计数",
                   rr["orphan_summary"](con, ttl_s=600)["orphan_tasks"] == 0)
        ok &= _rec("I6c active_runs 在空库返回 []", rr["active_runs"](con) == [])
        res = rr["reap_orphans"](con, ttl_s=600, audit=False)
        ok &= _rec("I6d reap 在空库返回 0 且不抛", res["reaped"] == 0, f"got={res}")
    except Exception as e:  # noqa: BLE001
        ok = _rec("I6 容错：不得抛异常", False, f"{type(e).__name__}: {e}")
    return ok


# ══════════════════ I7：summary 一致性 ══════════════════
def t_i7():
    print("\n=== I7 summary 与实际一致（含批次去重）===")
    rr = _load_rr()
    con = _db()
    for k in ("t1", "t2", "t3"):
        _seed(con, run=888, status="blocked", age_s=8000, task_key=k)
    _seed(con, run=889, status="ready", age_s=8000, task_key="t1")
    _seed(con, run=890, status="running", age_s=10, task_key="t1")     # 活的，不计入
    s = rr["orphan_summary"](con, ttl_s=600)
    ok = _rec("I7a 孤儿任务数 = 4", s["orphan_tasks"] == 4, f"got={s}")
    ok &= _rec("I7b 涉及批次 = 2（去重正确，且不含活跃批次 890）",
               s["orphan_runs"] == 2, f"got={s}")
    ok &= _rec("I7c 最老年龄 > TTL", s["oldest_age_s"] > 600, f"got={s['oldest_age_s']}")
    act = rr["active_runs"](con)
    ok &= _rec("I7d active_runs 含活跃批次并标注 is_orphan=False",
               any(a["run_id"] == 890 and a["is_orphan"] is False for a in act), f"got={act}")
    ok &= _rec("I7e active_runs 把超时的批次标注 is_orphan=True",
               any(a["run_id"] == 888 and a["is_orphan"] is True for a in act), f"got={act}")
    return ok


def main():
    print("=" * 78)
    print("P0-2 孤儿编排任务回收 —— 不变式 + 变异自证")
    print("=" * 78)
    t_i1()
    t_i2()
    m_i2()
    m_i2b()
    t_i3()
    t_i4()
    t_i5()
    t_i6()
    t_i7()

    n_fail = sum(1 for k, _, _ in _results if k == FAIL)
    n_vac = sum(1 for k, _, _ in _results if k == VACUOUS)
    print("\n" + "=" * 78)
    print(f"合计 {len(_results)} 项：PASS {len(_results)-n_fail-n_vac} / FAIL {n_fail} / VACUOUS {n_vac}")
    if n_vac:
        print("❌ 存在未被变异复现的断言（空转）")
    print("=" * 78)
    return 1 if (n_fail or n_vac) else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
