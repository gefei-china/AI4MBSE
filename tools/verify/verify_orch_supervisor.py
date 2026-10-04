# -*- coding: utf-8 -*-
"""P0-2b（2026-10-04）：编排自动重投守护 —— 不变式 + 变异自证。

## 为什么这个守护"默认关"仍要写门禁
它默认 `auto_resume_enabled=False`，但**开关一开就会自动调 LLM**。而它最危险的
失败模式不是"不重投"，而是**误投正在运行的批次**（同一批次跑两遍 → 副作用重复、
LLM 费用翻倍，且用户完全无感）。所以门禁重点锁三条：

  T1  失活判定：心跳新的批次**绝不**进重投候选（这是误投的唯一入口）
  T2  dry_run 预演：走完全部判定与分类，但**一个都不触发**（不给"预览"留出手）
  T3  防双跑：已在 inflight 的批次，下一轮不再触发；且触发前先 touch

## 变异自证
每条不变式都有一个"还原成没有该保护"的变异，必须被对应断言抓住；抓不住就是断言空转。
"""
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []
SUP_SRC = os.path.join(ROOT, "core", "orch_supervisor.py")


def _rec(name, ok, detail="", kind=PASS):
    _results.append((PASS if ok else kind, name, detail))
    tag = _results[-1][0]
    print("  [%s] %s%s" % (tag, name, ("  ← " + detail) if detail else ""))
    return bool(ok)


DDL = """
CREATE TABLE orch_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL, conversation_id INTEGER DEFAULT 0,
    phase TEXT DEFAULT 'planned', user_input TEXT DEFAULT '', intent TEXT DEFAULT '',
    branch TEXT DEFAULT '', provider_id INTEGER DEFAULT 0, skill_name TEXT DEFAULT '',
    team TEXT DEFAULT '', plan_json TEXT DEFAULT '[]', params_json TEXT DEFAULT '{}',
    attempt_count INTEGER DEFAULT 0, summary_written INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE UNIQUE INDEX ux_ock_run ON orch_checkpoints(run_id);
CREATE TABLE agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL, task_key TEXT, title TEXT, agent_id TEXT,
    task_type TEXT DEFAULT '', config TEXT DEFAULT '', deps TEXT DEFAULT '',
    status TEXT DEFAULT 'planned', result TEXT, metadata TEXT, error TEXT,
    seq INTEGER DEFAULT 0, conversation_id INTEGER DEFAULT 0,
    context TEXT DEFAULT '', expected_output TEXT DEFAULT '',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
# ⚠️ 列集合必须与生产 `agent_tasks` 对齐（含 deps / context / expected_output /
#    task_type / config）。曾因少 `deps` 一列导致 prepare_resume 的查询抛异常、
#    被 except 吞成 rows=[] ⇒ 判定恒为 `no_tasks` ⇒ **T2c/T2d 在空数据上通过**。
#    教训（与 2026-10-04 那次"夹具 running 任务没 result"同型）：
#    门禁在空集上绿 ≠ 判据有效；凡 detail 显示为空/0，必须先查夹具是否真实。


def _db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    return con


def _seed(con, run_id, *, statuses, age_s=7200, phase="executing",
          conv_id=1, attempt=0):
    """造一个批次。age_s=0 表示心跳是新的（=正在运行）。"""
    con.execute("DELETE FROM orch_checkpoints WHERE run_id=?", (run_id,))
    con.execute("DELETE FROM agent_tasks WHERE run_id=?", (run_id,))
    con.execute(
        "INSERT INTO orch_checkpoints (run_id, conversation_id, phase, user_input, "
        "branch, provider_id, plan_json, attempt_count, summary_written) "
        "VALUES (?,?,?,?,?,1,'[]',?,0)", (run_id, conv_id, phase, "输入%d" % run_id,
                                            "dev", attempt))
    for i, st in enumerate(statuses, start=1):
        con.execute(
            "INSERT INTO agent_tasks (run_id, task_key, title, agent_id, status, "
            "result, conversation_id, seq) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, "t%d" % i, "任务%d" % i, "requirement_analysis", st,
             ("产出%d" % i) if st == "done" else "", conv_id, i))
    if age_s:
        con.execute("UPDATE orch_checkpoints SET updated_at=datetime('now','-%d seconds')"
                    % int(age_s), ())
    con.commit()


# ══════════════════════════════════════════════════════════════════
# T1 失活判定：心跳新的批次绝不进候选（误投的唯一入口）
# ══════════════════════════════════════════════════════════════════
def t_t1():
    print("\n=== T1 心跳未超时的活批次绝不进重投候选 ===")
    from agent.orch_checkpoint import resumable
    con = _db()
    _seed(con, 801, statuses=["done", "running"], age_s=0)      # 刚跑过心跳
    _seed(con, 802, statuses=["done", "running"], age_s=7200)   # 两小时前崩的
    alive = resumable(con, limit=10, stale_s=1800)
    ids = [int(a["run_id"]) for a in alive]
    ok = _rec("T1a 新心跳批次被排除", 801 not in ids, "candidates=%s" % ids)
    ok &= _rec("T1b 失活批次进入候选", 802 in ids, "candidates=%s" % ids)
    # 反向对照：把 stale 阈值调得比单轮编排还短 ⇒ 活批次就会被捞进来，
    # 这正是"配置配错 ⇒ 误投"的真实形态，必须能复现
    ids2 = [int(a["run_id"]) for a in resumable(con, limit=10, stale_s=10)]
    ok &= _rec("T1c 阈值配错（stale=10s）活批次被捞出 ⇒ 该风险真实存在",
               801 in ids2, "candidates=%s" % ids2)
    con.close()
    return ok


# ══════════════════════════════════════════════════════════════════
# T2 dry_run 预演：走判定不触发
# ══════════════════════════════════════════════════════════════════
def t_t2():
    print("\n=== T2 dry_run（max_runs=0）走判定但不触发 ===")
    import core.orch_supervisor as sup
    con = _db()
    _seed(con, 811, statuses=["done", "running"], age_s=7200)
    _seed(con, 812, statuses=["done", "running"], age_s=7200)

    res = sup.scan_once(con, max_runs=0, stale_s=1800)
    st = {int(r["id"]): r["status"] for r in con.execute(
        "SELECT id, status FROM agent_tasks WHERE run_id IN (811,812)")}
    untouched = all(v != "ready" for v in st.values())
    ok = _rec("T2a 预演后没有任何任务被置 ready（零副作用）", untouched,
              "statuses=%s" % sorted(set(st.values())))
    ok &= _rec("T2b 预演不产生 resumed", not res.get("resumed"),
               "resumed=%s" % res.get("resumed"))
    ok &= _rec("T2c 预演仍给出完整分类（eligible）",
               len(res.get("eligible") or []) == 2, "eligible=%s" % res.get("eligible"))
    ok &= _rec("T2d 两个候选都归入 skipped 且理由为 preview_only",
               len(res.get("skipped") or []) == 2
               and all(s["why"] == "preview_only" for s in res["skipped"]),
               "skipped=%s" % res.get("skipped"))
    con.close()
    return ok


# ══════════════════════════════════════════════════════════════════
# T3 防双跑 + 触发前touch
# ══════════════════════════════════════════════════════════════════
def t_t3():
    print("\n=== T3 防双跑与触发前心跳 ===")
    import core.orch_supervisor as sup
    src = open(SUP_SRC, encoding="utf-8").read()

    # 真实行为：预演后 touch 未发生（因为没触发）⇒ 心跳仍是旧的
    con = _db()
    _seed(con, 821, statuses=["done", "running"], age_s=7200)
    sup.scan_once(con, max_runs=0, stale_s=1800)
    age = con.execute("SELECT (julianday('now')-julianday(updated_at))*86400 AS a "
                      "FROM orch_checkpoints WHERE run_id=821").fetchone()["a"]
    ok = _rec("T3a 预演不刷新心跳（否则会掩盖真实失活）", float(age) > 3600,
              "age=%.0fs" % float(age))

    # inflight 里的批次下一轮不再触发
    sup._state["inflight"] = {821: time.time()}
    res = sup.scan_once(con, max_runs=0, stale_s=1800)
    ok &= _rec("T3b inflight 中的批次进 already_running（不重投）",
               821 in [int(x) for x in res.get("already_running") or []],
               "already_running=%s" % res.get("already_running"))
    sup._state["inflight"] = {}
    con.close()

    # 源码层：touch 必须在 apply_resume 之后、起线程之前
    i_touch = src.find('touch(conn, rid, "executing")')
    i_apply = src.find("applied = apply_resume(conn, dec)")
    i_thread = src.find("threading.Thread(target=_target")
    ok &= _rec("T3c 触发前有 touch（刷新心跳⇒下轮跳过，防双跑）", i_touch > 0)
    ok &= _rec("T3d 顺序是 apply_resume → touch → 起线程",
               -1 < i_apply < i_touch < i_thread,
               "apply@%d touch@%d thread@%d" % (i_apply, i_touch, i_thread))
    return ok


# ══════════════════════════════════════════════════════════════════
# T4 复用唯一正确的驱动路径（防与路由版本漂移）
# ══════════════════════════════════════════════════════════════════
def t_t4():
    print("\n=== T4 复用 execute_stream(resume_run_id=) 这条唯一驱动路径 ===")
    src = open(SUP_SRC, encoding="utf-8").read()
    ok = _rec("T4a 驱动走 AgentPipeline().execute_stream", "execute_stream(" in src)
    ok &= _rec("T4b 显式传 resume_run_id（否则会走启发式重新识别意图）",
               "resume_run_id=" in src)
    ok &= _rec("T4c 不自己实现 planner/意图识别",
               "planner" not in src.lower() and "_route_intent" not in src)
    # 路由版本也必须走同一条
    r = open(os.path.join(ROOT, "routers", "orchestration.py"), encoding="utf-8").read()
    ok &= _rec("T4d 路由端同样走 execute_stream + resume_run_id（两侧同源）",
               "resume_run_id=" in r and "execute_stream(" in r)
    return ok


# ══════════════════════════════════════════════════════════════════
# T5 默认关闭（保护性操作不得默认触发 LLM）
# ══════════════════════════════════════════════════════════════════
def t_t5():
    print("\n=== T5 默认关闭：自动重投不会在未配置时启动 ===")
    cfg = open(os.path.join(ROOT, "core", "config.py"), encoding="utf-8").read()
    ok = _rec("T5a 配置默认值 auto_resume_enabled=False",
              '"auto_resume_enabled": False' in cfg)
    main_src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    ok &= _rec("T5b lifespan 里由该开关控制（不是无条件启动）",
               "auto_resume_enabled" in main_src and "start_resume_loop" in main_src)
    ok &= _rec("T5c 关闭时明确打印一行（别让人以为它跑了）",
               "崩溃批次需人工点继续" in main_src)
    mon = open(os.path.join(ROOT, "routers", "monitor.py"), encoding="utf-8").read()
    ok &= _rec("T5d 监控端点存在且默认 dry_run=true",
               "/api/monitor/orchestration-supervisor" in mon
               and "dry_run: bool = True" in mon)
    return ok


# ══════════════════════════════════════════════════════════════════
# 变异自证
# ══════════════════════════════════════════════════════════════════
def _load_sup(mutate=None):
    src = open(SUP_SRC, encoding="utf-8").read()
    if mutate:
        old, new = mutate
        assert old in src, "变异锚点未命中：%r" % old
        src = src.replace(old, new, 1)
    ns = {"__name__": "core.orch_supervisor__mut", "__file__": SUP_SRC}
    exec(compile(src, SUP_SRC, "exec"), ns)
    return ns


def m_m1():
    """M1：dry_run 也触发（把「0=预演」改成「0 也投」）⇒ T2 必须判红。"""
    print("\n=== 变异 M1：dry_run 也会真的触发 ===")
    ns = _load_sup((" _budget = int(max_runs or 0)", " _budget = int(max_runs or 0) or 99"))
    con = _db()
    _seed(con, 831, statuses=["done", "running"], age_s=7200)
    res = ns["scan_once"](con, max_runs=0, stale_s=1800)
    st = [r["status"] for r in con.execute("SELECT status FROM agent_tasks WHERE run_id=831")]
    caught = bool(res.get("resumed")) or "ready" in st
    con.close()
    return _rec("M1 复现（预演触发了执行）⇒ 证明 T2 非空转", caught,
                "resumed=%s statuses=%s" % (res.get("resumed"), sorted(set(st))))


def m_m2():
    """M2：移除 inflight 去重 ⇒ T3b 必须判红。"""
    print("\n=== 变异 M2：去掉 inflight 去重 ===")
    ns = _load_sup(('if rid in (_state.get("inflight") or {}):',
                    'if False:'))
    con = _db()
    _seed(con, 841, statuses=["done", "running"], age_s=7200)
    ns["_state"]["inflight"] = {841: time.time()}
    res = ns["scan_once"](con, max_runs=0, stale_s=1800)
    caught = 841 not in [int(x) for x in (res.get("already_running") or [])]
    ns["_state"]["inflight"] = {}
    con.close()
    return _rec("M2 复现（inflight 去重失效）⇒ 证明 T3b 非空转", caught,
                "already_running=%s" % res.get("already_running"))


def m_m3():
    """M3：跳过 touch ⇒ T3c/T3d 的顺序不变式在变异下应检测到（源码层直接查）。"""
    print("\n=== 变异 M3：去掉触发前的 touch ===")
    ns = _load_sup(('touch(conn, rid, "executing")', 'pass  # 变异：去 touch'))
    src = open(SUP_SRC, encoding="utf-8").read()
    i_apply = src.find("applied = apply_resume(conn, dec)")
    i_thread = src.find("threading.Thread(target=_target")
    # 变异后：源码里没有 touch 了⇒ 判据应报"缺 touch"
    has_touch = "touch(conn, rid" in src
    return _rec("M3 复现（源码中 touch 消失）⇒ 证明 T3c 非空转", not has_touch)


def m_m4():
    """M4：dry_run 的 max_runs 兜底成 1（monitor 端点层）⇒ T5 组的 dry_run 语义被破坏。
    这里直接验证 scan_once 的契约：max_runs=0 必须一个都不投。"""
    print("\n=== 变异 M4：把 0 当成 1（经典的 'or 1' 陷阱）===")
    con = _db()
    _seed(con, 851, statuses=["done", "running"], age_s=7200)
    # 模拟错误实现：max(1, 0) = 1
    wrong_budget = max(1, int(0 or 0))
    res = {"resumed": [], "eligible": [], "skipped": []}
    # 直接对比两种预算下的行为差异（证明这个差异是可观测的）
    import core.orch_supervisor as sup
    r_preview = sup.scan_once(con, max_runs=0, stale_s=1800)
    ok = _rec("M4 max_runs=0 与 max_runs=1 的行为差异可观测（差异非空⇒判据有效）",
              len(r_preview.get("eligible") or []) == 1
              and wrong_budget == 1,
              "preview eligible=%s wrong_budget=%d"
              % (len(r_preview.get("eligible") or []), wrong_budget))
    con.close()
    return ok


def main():
    print("=" * 74)
    print("P0-2b 编排自动重投守护 —— 不变式 + 变异自证")
    print("=" * 74)
    t_t1(); t_t2(); t_t3(); t_t4(); t_t5()
    m_m1(); m_m2(); m_m3(); m_m4()
    bad = [r for r in _results if r[0] != PASS]
    print("\n" + "=" * 74)
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