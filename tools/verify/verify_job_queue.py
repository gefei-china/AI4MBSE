# -*- coding: utf-8 -*-
"""P0-C：进程内异步作业队列 —— 不变式 + 变异自证。

## 三条核心不变式（每条配变异自证）
Q1 **提交即返回**：submit() 的耗时与 handler 执行时长**无关**；幂等键重复提交
   返回**同一个 job_id** 且不重复入队（防"用户手抖点两下"）。
Q2 **租约回收**：worker 崩掉后（租约过期）的 running 作业能被**下一个 worker 领取**。
   ⚠️ 这条不能只测 `status='running'` 会不会被捞 —— 真正的判据是
   **"租约未过期的 running 绝不能被另一个 worker 领走"**（否则双跑、副作用重复）。
Q3 **重试有上限**：失败且 attempt < max_attempts ⇒ 回 queued；超限 ⇒ failed。
   连续失败作业不得无限循环（自愈死循环）。

## 变异自证
每条变异**改行为**（不是改字符串），再用同一个判据函数真跑一遍。
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []
_MUT_RED = []
_IN_MUT = [False]
JQ_SRC = os.path.join(ROOT, "core", "job_queue.py")

DDL = """
CREATE TABLE IF NOT EXISTS job_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT UNIQUE NOT NULL,
    kind TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued',
    progress INTEGER DEFAULT 0,
    stage TEXT DEFAULT '',
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    attempt INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    lease_until TEXT DEFAULT '',
    heartbeat_at TEXT DEFAULT '',
    worker_id TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_job_pick ON job_jobs(status, created_at);
"""


def _rec(name, ok, detail="", kind=FAIL):
    rec = (PASS if ok else kind, name, detail)
    (_MUT_RED if _IN_MUT[0] else _results).append(rec)
    print("  [%s] %s%s" % (rec[0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    return con


def _load(mutate=None):
    """加载 job_queue（可带变异）。

    ⚠️ 只替换 `pick_next` / `report` / `submit` 等**纯函数**，不碰 `run_worker_loop`
    —— worker循环依赖 database.get_db()，在测试里换库成本高（connection.py 在
    import 期绑定 DB_PATH）；核心判定全部在前三者里，已足够覆盖三条不变式。
    """
    src = open(JQ_SRC, encoding="utf-8").read()
    if mutate:
        old, new = mutate
        assert old in src, "变异锚点未命中：%r" % old
        src = src.replace(old, new, 1)
    ns = {"__name__": "core.job_queue__mut", "__file__": JQ_SRC}
    exec(compile(src, JQ_SRC, "exec"), ns)
    return ns


# ══════════════════════════════════════════════════════════════════
# Q1 提交即返回 + 幂等
# ══════════════════════════════════════════════════════════════════
def t_q1(ns):
    print("\n=== Q1 提交即返回 / 幂等 ===")
    con = _db()
    t0 = time.time()
    r1 = ns["submit"](con, "slow_kind", {"x": 1}, job_key="k1")
    el = time.time() - t0
    ok = _rec("Q1a submit 不等 handler（毫秒级返回）", el < 0.2,
              "elapsed=%.4fs" % el)
    ok &= _rec("Q1b submit 返回 job_id 且状态 queued",
               isinstance(r1.get("job_id"), int) and r1["job_id"] > 0
               and r1["status"] == "queued", str(r1))
    r2 = ns["submit"](con, "slow_kind", {"x": 1}, job_key="k1")
    ok &= _rec("Q1c 同幂等键重复提交 ⇒ 返回同一 job_id", r2["job_id"] == r1["job_id"],
               "%s vs %s" % (r1["job_id"], r2["job_id"]))
    ok &= _rec("Q1d 重复提交标记 deduped=True", r2.get("deduped") is True)
    n = con.execute("SELECT COUNT(*) FROM job_jobs").fetchone()[0]
    ok &= _rec("Q1e 表里只有一行（未重复入队）", n == 1, "rows=%d" % n)
    # 不同键⇒ 两个作业
    ns["submit"](con, "slow_kind", {"x": 2}, job_key="k2")
    n2 = con.execute("SELECT COUNT(*) FROM job_jobs").fetchone()[0]
    ok &= _rec("Q1f 不同幂等键 ⇒ 两个作业", n2 == 2, "rows=%d" % n2)
    # 无幂等键时自动生成（不得撞车）
    a = ns["submit"](con, "k", {})
    b = ns["submit"](con, "k", {})
    ok &= _rec("Q1g 无幂等键时各自独立（不误去重）",
               a["job_id"] != b["job_id"], "%s/%s" % (a["job_id"], b["job_id"]))
    con.close()
    return ok


# ══════════════════════════════════════════════════════════════════
# Q2 租约：未过期不被抢 / 过期可回收
# ══════════════════════════════════════════════════════════════════
def t_q2(ns):
    print("\n=== Q2 租约：未过期不被抢、过期可回收 ===")
    con = _db()
    jid = ns["submit"](con, "k", {}, job_key="q2")["job_id"]
    # 领一次（写租约）
    job = ns["pick_next"](con, worker_id="w1", lease_s=300)
    ok = _rec("Q2a 首次领取成功且状态转 running",
              job and int(job["id"]) == jid and job["status"] == "running",
              str({k: job[k] for k in ("id", "status", "worker_id")}) if job else "None")
    # 第二个 worker **不应**领到（租约未过期）
    job2 = ns["pick_next"](con, worker_id="w2", lease_s=300)
    stolen = bool(job2 and int(job2["id"]) == jid)
    ok &= _rec("Q2b 租约未过期 ⇒ 另一 worker 领不到（防双跑）", not stolen,
               "w2 拿到=%s" % (job2["id"] if job2 else None))
    # 把租约改成过去 ⇒ 应可回收
    con.execute("UPDATE job_jobs SET lease_until='2000-01-01 00:00:00' WHERE id=?", (jid,))
    con.commit()
    job3 = ns["pick_next"](con, worker_id="w3", lease_s=300)
    ok &= _rec("Q2c 租约过期 ⇒ 可被回收（崩溃后不永久卡住）",
               bool(job3 and int(job3["id"]) == jid),
               "w3 拿到=%s" % (job3["id"] if job3 else None))
    ok &= _rec("Q2d 回收时 worker 换成新的", bool(job3) and job3.get("worker_id") == "w3",
               "worker=%s" % (job3.get("worker_id") if job3 else None))
    ok &= _rec("Q2e 回收使 attempt 递增（便于观察被重试过几次）",
               bool(job3) and int(job3.get("attempt") or 0) >= 2,
               "attempt=%s" % (job3.get("attempt") if job3 else None))
    # lease_expired 纯函数
    ok &= _rec("Q2f lease_expired：空租约视为过期",
               ns["lease_expired"]({"lease_until": ""}) is True)
    ok &= _rec("Q2g lease_expired：未来租约未过期",
               ns["lease_expired"]({"lease_until": "2099-01-01 00:00:00"}) is False)
    ok &= _rec("Q2h lease_expired：过去租约已过期",
               ns["lease_expired"]({"lease_until": "2000-01-01 00:00:00"}) is True)
    ok &= _rec("Q2i lease_expired：垃圾输入视为过期（宁可回收一次）",
               ns["lease_expired"]({"lease_until": "not-a-date"}) is True)
    con.close()
    return ok


# ══════════════════════════════════════════════════════════════════
# Q3 重试上限 / 终态 / 进度
# ══════════════════════════════════════════════════════════════════
def t_q3(ns):
    print("\n=== Q3 重试有上限 / 终态不可变 ===")
    con = _db()
    jid = ns["submit"](con, "k", {}, job_key="q3", max_attempts=3)["job_id"]
    # attempt=1 时失败 ⇒ 回 queued（可重试）
    ns["pick_next"](con, worker_id="w1", lease_s=300)     # attempt → 1
    r = ns["report"](con, jid, False, error="第一次失败")
    j = ns["get"](con, jid)
    ok = _rec("Q3a 未超上限 ⇒ 回 queued 而非 failed",
              j["status"] == "queued", "status=%s" % j["status"])
    ok &= _rec("Q3b 回报里带 retry 计数", int(r.get("retry") or 0) == 1, str(r))
    # 继续失败直到超限
    ns["pick_next"](con, worker_id="w1", lease_s=300)     # attempt → 2
    ns["report"](con, jid, False, error="第二次失败")
    j = ns["get"](con, jid)
    ok &= _rec("Q3c attempt=2/3 仍回 queued", j["status"] == "queued",
               "status=%s attempt=%s" % (j["status"], j["attempt"]))
    ns["pick_next"](con, worker_id="w1", lease_s=300)     # attempt → 3
    ns["report"](con, jid, False, error="第三次失败")
    j = ns["get"](con, jid)
    ok &= _rec("Q3d 超上限 ⇒ failed（不自愈死循环）", j["status"] == "failed",
               "status=%s attempt=%s" % (j["status"], j["attempt"]))
    ok &= _rec("Q3d2 恰好在 attempt==max_attempts 时转 failed（判据边界）",
               int(j["attempt"]) == 3, "attempt=%s" % j["attempt"])
    ok &= _rec("Q3e 失败原因落库可诊断", "第三次失败" in (j["error"] or ""),
               (j["error"] or "")[:40])
    ok &= _rec("Q3f failed 后不再被领取（终态）",
               ns["pick_next"](con, worker_id="w9", lease_s=300) is None)
    # 成功路径
    j2 = ns["submit"](con, "k", {}, job_key="q3b")["job_id"]
    ns["pick_next"](con, worker_id="w1", lease_s=300)
    ns["report"](con, j2, True, result={"v": 42})
    j = ns["get"](con, j2)
    ok &= _rec("Q3g 成功 ⇒ done 且 progress=100",
               j["status"] == "done" and int(j["progress"]) == 100,
               "status=%s progress=%s" % (j["status"], j["progress"]))
    ok &= _rec("Q3h 结果落库可取回", "42" in (j["result"] or ""), (j["result"] or "")[:30])
    ok &= _rec("Q3i 两个作业都进入终态（done + failed）",
               (ns["get"](con, jid) or {}).get("status") == "failed"
               and (ns["get"](con, j2) or {}).get("status") == "done",
               "failed=%s done=%s" % ((ns["get"](con, jid) or {}).get("status"),
                                       (ns["get"](con, j2) or {}).get("status")))
    # 进度上报不影响终态判定
    ns["progress"](con, j2, 50, "中途")
    ok &= _rec("Q3j progress 上报后状态不变（进度只是给人看的）",
               ns["get"](con, j2)["status"] == "done")
    con.close()
    return ok


# ══════════════════════════════════════════════════════════════════
# Q4 取消 / 统计 / worker 契约
# ══════════════════════════════════════════════════════════════════
def t_q4(ns):
    print("\n=== Q4 取消与统计 ===")
    con = _db()
    jid = ns["submit"](con, "k", {}, job_key="q4")["job_id"]
    r = ns["cancel"](con, jid)
    ok = _rec("Q4a queued 作业可取消", r.get("ok") is True
              and ns["get"](con, jid)["status"] == "canceled", str(r))
    ok &= _rec("Q4b 取消不存在的作业报错而非静默",
               ns["cancel"](con, 99999).get("ok") is False)
    # running 且租约未过期 ⇒ 拒绝取消（防留下半个副作用）
    j2 = ns["submit"](con, "k", {}, job_key="q4b")["job_id"]
    ns["pick_next"](con, worker_id="w1", lease_s=300)
    r2 = ns["cancel"](con, j2)
    ok &= _rec("Q4c 正在执行（租约有效）的作业拒绝取消", r2.get("ok") is False,
               str(r2)[:80])
    ok &= _rec("Q4d 拒绝取消后状态仍 running",
               ns["get"](con, j2)["status"] == "running")
    # 已终态不可取消
    r3 = ns["cancel"](con, jid)
    ok &= _rec("Q4e 终态作业不可取消", r3.get("ok") is False)
    # 统计
    st = ns["queue_stats"](con)
    ok &= _rec("Q4f 统计含各状态计数",
               st.get("canceled") == 1 and st.get("running") == 1, str(st))
    ok &= _rec("Q4g 统计给出最老活跃作业时间（可观测）",
               "oldest_active" in st, str(st)[:100])
    # handler 注册表
    ok &= _rec("Q4h register_handler 可注册（供 worker 分派）",
               ns["register_handler"]("t_kind", lambda p, j: {}) is None)
    con.close()
    return ok


# ══════════════════════════════════════════════════════════════════
# 变异自证
# ══════════════════════════════════════════════════════════════════
def _run_in_mut(fn, ns, *args):
    """在变异命名空间下跑一遍不变式组，返回**变红的断言列表**。

    ⚠️ 变异后代码可能**抛异常**（如幂等失效⇒ 撞 UNIQUE 约束）。
    这同样是"行为已变"的证据，不能让异常中断整个门禁 —— 记为一条红灯。
    """
    global _MUT_RED
    _MUT_RED = []
    _IN_MUT[0] = True
    try:
        fn(ns, *args)
    except Exception as e:
        _MUT_RED.append((FAIL, "变异导致执行中断（也是行为已变的证据）",
                         "%s: %s" % (type(e).__name__, str(e)[:80])))
    finally:
        _IN_MUT[0] = False
    return [r for r in _MUT_RED if r[0] != PASS]


def d1():
    """D1：幂等失效（总是新插入）⇒ Q1c/Q1d/Q1e 必须判红。"""
    print("\n=== 变异 D1：幂等键失效（每次都新建）===")
    ns = _load(('    if k:\n        r = conn.execute(\n'
                '            "SELECT id, status, progress, stage FROM job_jobs WHERE job_key=? LIMIT 1",\n'
                '            (k,)).fetchone()',
                '    if False:\n        r = conn.execute(\n'
                '            "SELECT id, status, progress, stage FROM job_jobs WHERE job_key=? LIMIT 1",\n'
                '            (k,)).fetchone()'))
    reds = _run_in_mut(t_q1, ns)
    # 判据有两种等价形态：①断言变红 ②执行中断（撞 UNIQUE 约束 =幂等键确实存在）
    hit = any(("Q1c" in n or "Q1e" in n) or "中断" in n for _k, n, _d in reds)
    return _rec("D1 复现（幂等失效 ⇒ Q1c/Q1e 判红或撞 UNIQUE 约束）⇒ 证明 Q1 非空转", hit,
                "变异组内红灯 %d 条：%s" % (len(reds), [n for _k, n, _d in reds][:3]))


def d2():
    """D2：租约检查失效（任何时候都能被别人领）⇒ Q2b 必须判红。"""
    print("\n=== 变异 D2：租约检查失效（running 也可被抢）===")
    # ⚠️ 变异锚点必须落在"回收失活running"那一段（第二段循环）：
    #   Q2b 测的是"**已running 且租约有效**的作业不被另一个 worker 领走"，
    #   它走的是第二段（第一段只扫 status='queued'，捞不到 running 的）。
    #   第一版把锚点改在第一段的租约判断上 ⇒ 变异没生效 ⇒ Q2b 不红（空转）。
    ns = _load(('    for r in conn.execute(\n'
                '            "SELECT id FROM job_jobs WHERE status=\'running\' AND "\n'
                '            "(lease_until=\'\' OR lease_until < ?) LIMIT 1", (now,)).fetchall():',
                '    for r in conn.execute(\n'
                '            "SELECT id FROM job_jobs WHERE status=\'running\' LIMIT 1").fetchall():'))
    reds = _run_in_mut(t_q2, ns)
    hit = any("Q2b" in n for _k, n, _d in reds)
    return _rec("D2 复现（租约未过期也能被抢）⇒ 证明 Q2b 非空转", hit,
                "变异组内红灯 %d 条" % len(reds))


def d3():
    """D3：重试无上限（永远回 queued）⇒ Q3d 必须判红。"""
    print("\n=== 变异 D3：重试无上限（永远回 queued）===")
    ns = _load(("    if attempt < maxa:", "    if True:"))
    reds = _run_in_mut(t_q3, ns)
    hit = any("Q3d" in n for _k, n, _d in reds)
    return _rec("D3 复现（永不转 failed ⇒ 自愈死循环）⇒ 证明 Q3d 非空转", hit,
                "变异组内红灯 %d 条" % len(reds))


def d4():
    """D4：取消不做租约检查 ⇒ Q4c 必须判红。"""
    print("\n=== 变异 D4：取消时不看租约 ===")
    ns = _load(('    if job["status"] == "running" and not lease_expired(job):',
                '    if False:'))
    reds = _run_in_mut(t_q4, ns)
    hit = any("Q4c" in n for _k, n, _d in reds)
    return _rec("D4 复现（正在执行的也能被取消）⇒ 证明 Q4c 非空转", hit,
                "变异组内红灯 %d 条" % len(reds))


def main():
    print("=" * 76)
    print("P0-C 进程内异步作业队列 —— 不变式 + 变异自证")
    print("=" * 76)
    ns = _load()
    t_q1(ns); t_q2(ns); t_q3(ns); t_q4(ns)
    d1(); d2(); d3(); d4()
    bad = [r for r in _results if r[0] != PASS]
    print("\n" + "=" * 76)
    print("断言总数 %d  PASS %d  FAIL/VACUOUS %d"
          % (len(_results), len(_results) - len(bad), len(bad)))
    if bad:
        for k, n, d in bad:
            print("  [%s] %s  %s" % (k, n, d))
        print("\n结论：有断言未通过 / 至少一组变异未被复现（空转）")
        return 1
    print("结论：全部通过，且 4 组变异均被复现（断言非空转）")
    return 0


if __name__ == "__main__":
    sys.exit(main())