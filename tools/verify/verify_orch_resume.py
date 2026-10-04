# -*- coding: utf-8 -*-
"""P0-2 第二步（编排 checkpoint + 断点续跑）回归验证 —— 零 LLM、独立临时库，可反复跑。

**为什么需要它**（承接批次 1 §7.2 依赖顺序的 B 步）：
    批次 1 只做了一半 —— 能"识别死掉的运行"并标记 failed，但**死掉的运行依然只能从头再来**：
    `agent_tasks` 里虽有每个子任务的行，却没人记录这次编排原本的 **输入/意图/分支/provider/阶段**，
    于是「重发一遍用户原话」成了唯一入口 —— 而那会①重跑意图识别（启发式结论可能不同）
    ②再调一次 planner LLM（得到**不同的任务拆分**，旧结果按 key 复用失败）③可能触发澄清拦截。
    对标 LangGraph 的 `checkpointer + resume`：同一张图 + 恢复的状态，而不是"再跑一次"。

本脚本锁定的不变式：
    I1  检查点 save/load 往返一致（含 plan JSON 保真）
    I2  心跳节流：高频 touch 不刷爆写；静默期过后确实刷新
    I3  存活判定：心跳新 ⇒ alive；超时 ⇒ 不 alive；终态 phase ⇒ 永不 alive
    I4  **幂等红线**：任何策略组合下 `done` 任务都**不得**进入重排队集合（这是"不能重复写实体"的根本）
    I5  running 任务：有活进程 ⇒ owned（拒绝接管）；无主 ⇒ requeue
    I6  failed 任务默认 skip，仅 include_failed=True 才 requeue
    I7  apply_resume 只改 status/error；**不清** result/metadata/context（否则丢失已交付内容）
    I8  prepare_resume 六道闸门逐个生效（no_checkpoint / terminal / attempt / alive / no_tasks / nothing_to_do）
    I9  端到端：4 任务批次中 2 个已 done、1 running、1 blocked ⇒ resume 只重排队后两个
    I10 接线契约：stream.py 在 resume 下**跳过 create_plan 且不调 planner**
    I11 时区口径：所有时间比较在 SQL 内完成（不得把 SQLite UTC 时间戳取回 Python 做差）
    I12 配置与建表：orchestration 配置段存在；init_db 后 orch_checkpoints 可用

**变异自证（强制）**：把"错写法"注入源码，脚本必须判 FAIL/VACUOUS。
   本脚本内置 5 组变异，其中最关键的是 M1（让 done 任务也被重跑）——
   那是本功能唯一会造成**不可逆数据重复**的错误。

用法（**裸跑自身即完整口径**）：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_orch_resume.py
退出码：全绿 0 / 有失败或空转 1。
"""
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

CK_SRC = os.path.join(ROOT, "agent", "orch_checkpoint.py")
STREAM_SRC = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((kind if not ok else PASS, name, detail))
    print(f"  {PASS if ok else kind}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


DDL_TASKS = """CREATE TABLE agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER DEFAULT 0, task_key TEXT DEFAULT '', title TEXT DEFAULT '',
    agent_id TEXT DEFAULT '', task_type TEXT DEFAULT 'agent',
    config TEXT DEFAULT '{}', deps TEXT DEFAULT '[]', status TEXT DEFAULT 'planned',
    result TEXT DEFAULT '', metadata TEXT DEFAULT '{}', error TEXT DEFAULT '',
    assigned_by TEXT DEFAULT 'planner', seq INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    context TEXT DEFAULT '', expected_output TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0)"""

DDL_CK = """CREATE TABLE orch_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL, conversation_id INTEGER DEFAULT 0,
    phase TEXT DEFAULT 'planned', user_input TEXT DEFAULT '', intent TEXT DEFAULT '',
    branch TEXT DEFAULT '', provider_id INTEGER DEFAULT 0, skill_name TEXT DEFAULT '',
    team TEXT DEFAULT '', plan_json TEXT DEFAULT '[]', params_json TEXT DEFAULT '{}',
    attempt_count INTEGER DEFAULT 0, summary_written INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"""

# C-1 需要：summary_only_eligible 会 JOIN messages 查"该 run 名下是否已有 assistant 消息"。
# 缺这张表时探测走 except → probe_failed → 拒绝恢复，断言会**假绿**（永远走不到放行分支）。
DDL_MSG = """CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER DEFAULT 0, role TEXT DEFAULT 'user', content TEXT DEFAULT '',
    msg_type TEXT DEFAULT 'text', card_data TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""


def _db(stale_s=0.0):
    """独立内存库；stale_s>0 时把检查点心跳人为回拨到过去（模拟崩溃现场）。"""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(DDL_TASKS)
    con.execute(DDL_CK)
    con.execute(DDL_MSG)
    con.execute("CREATE UNIQUE INDEX ux_ock_run ON orch_checkpoints(run_id)")
    if stale_s:
        con.execute("UPDATE orch_checkpoints SET updated_at="
                    "datetime('now','-%d seconds')" % int(stale_s))
        con.commit()
    return con


def _seed(con, run_id=901, conv=77, plan=None, statuses=("planned",), with_ck=True):
    """落一批任务；statuses 逐条指定状态。"""
    tasks = []
    for i, st in enumerate(statuses, start=1):
        cur = con.execute(
            "INSERT INTO agent_tasks (run_id, task_key, title, agent_id, status, deps, "
            "conversation_id, seq, context, result, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, "t%d" % i, "任务%d" % i, "requirement_analysis", st, "[]",
             conv, i, "ctx-%d" % i, ("结果%d" % i) if st == "done" else "", '{"k":%d}' % i))
        tasks.append(int(cur.lastrowid))
    con.commit()
    if with_ck:
        save(con, run_id, conversation_id=conv, phase=plan and plan.get("_phase") or "executing",
             user_input="原始用户输入", intent="requirement_analysis", branch="release",
             provider_id=3, plan=plan if isinstance(plan, list) else
             [{"key": "t%d" % i, "title": "任务%d" % i} for i in range(1, len(statuses) + 1)])
    return tasks


def _reload(mut=None):
    """把 orch_checkpoint.py 重新加载；mut=(old,new) 做源码变异后执行。"""
    src = open(CK_SRC, encoding="utf-8").read()
    if mut:
        old, new = mut[0], mut[1]
        assert old in src, "变异锚点丢失: %r" % old[:60]
        src = src.replace(old, new, 1)
    ns = {"__name__": "orch_checkpoint_mut"}
    exec(compile(src, CK_SRC, "exec"), ns)
    return ns


# 用真实源码执行一次，拿到被测符号（后续不变式都从源码里取，不手写复刻）
CK = _reload()
save = CK["save"]
load = CK["load"]
touch = CK["touch"]
policy_classify = CK["policy_classify"]
pick_requeue_ids = CK["pick_requeue_ids"]
prepare_resume = CK["prepare_resume"]
apply_resume = CK["apply_resume"]


def t_i1():
    print("\n=== I1 检查点往返：plan JSON 保真、终态可辨 ===")
    con = _db()
    plan = [{"key": "t1", "title": "需求抽取", "agent": "ra"},
            {"key": "t2", "title": "建模", "deps": ["t1"]}]
    ok = _rec("I1a save 返回成功", save(con, 901, conversation_id=77, phase="planned",
                                     user_input="做个需求模型", intent="requirement_analysis",
                                     branch="release", provider_id=3, plan=plan) is True)
    ck = load(con, 901)
    ok &= _rec("I1b load 命中", ck is not None)
    ok &= _rec("I1c plan JSON 逐字保真", ck and ck.get("plan") == plan, f"got={ck and ck.get('plan')}")
    ok &= _rec("I1d 坐标系字段齐备",
               ck and ck.get("user_input") == "做个需求模型" and ck.get("branch") == "release"
               and ck.get("provider_id") == 3 and ck.get("intent") == "requirement_analysis")
    # 幂等 UPSERT：重复 save 不产生第二行，且不覆盖已写入的实值
    save(con, 901, phase="executing")
    n = con.execute("SELECT COUNT(*) FROM orch_checkpoints WHERE run_id=901").fetchone()[0]
    ok &= _rec("I1e 重复 save 仍是单行（UPSERT）", n == 1, f"rows={n}")
    ck2 = load(con, 901)
    ok &= _rec("I1f 二次 save 未把已存实值清空",
               ck2 and ck2.get("user_input") == "做个需求模型" and ck2.get("phase") == "executing")
    return ok


def t_i2():
    print("\n=== I2 心跳节流（不把 SQLite 单写者打满）===")
    con = _db()
    _seed(con, statuses=("planned",))
    before = con.execute("SELECT updated_at FROM orch_checkpoints WHERE run_id=901").fetchone()[0]
    time.sleep(1.05)
    for _ in range(20):
        touch(con, 901, None, every_s=30.0)
    after = con.execute("SELECT updated_at FROM orch_checkpoints WHERE run_id=901").fetchone()[0]
    ok = _rec("I2a 节流期内 20 次 touch 不刷新", before == after, f"{before} -> {after}")
    touch(con, 901, None, every_s=0.5)
    after2 = con.execute("SELECT updated_at FROM orch_checkpoints WHERE run_id=901").fetchone()[0]
    ok &= _rec("I2b 超过节流窗后确实刷新", after2 != after, f"{after} -> {after2}")
    touch(con, 901, "summarizing")
    phase = con.execute("SELECT phase FROM orch_checkpoints WHERE run_id=901").fetchone()[0]
    ok &= _rec("I2c touch 可同时推进 phase", phase == "summarizing", f"phase={phase}")
    return ok


def t_i3():
    print("\n=== I3 存活判定（UTC 口径，时间比较在 SQL 内）===")
    con = _db()
    _seed(con, statuses=("running",))
    ok = _rec("I3a 心跳新鲜 ⇒ alive", CK["is_alive"](con, 901, stale_s=1800) is True)
    con.execute("UPDATE orch_checkpoints SET updated_at=datetime('now','-3600 seconds') WHERE run_id=901")
    con.commit()
    ok &= _rec("I3b 心跳超时 ⇒ 不 alive", CK["is_alive"](con, 901, stale_s=1800) is False)
    con.execute("UPDATE orch_checkpoints SET phase='done', updated_at=CURRENT_TIMESTAMP WHERE run_id=901")
    con.commit()
    ok &= _rec("I3c 终态 phase ⇒ 永不 alive", CK["is_alive"](con, 901, stale_s=1800) is False)
    ok &= _rec("I3d 无检查点 ⇒ 不 alive", CK["is_alive"](con, 999, stale_s=1800) is False)
    return ok


def t_i4():
    print("\n=== I4 幂等红线：done 任务永不被重跑 ===")
    ok = True
    for inc in (False, True):
        for owner in (False, True):
            v = policy_classify("done", run_has_owner=owner, include_failed=inc)
            ok &= _rec(f"I4  done 恒 reuse（include_failed={inc}, owner={owner}）",
                       v == "reuse", f"got={v}")
    tasks = [{"id": 1, "task_key": "t1", "status": "done"},
             {"id": 2, "task_key": "t2", "status": "running"},
             {"id": 3, "task_key": "t3", "status": "blocked"}]
    rq, reuse, skip = pick_requeue_ids(tasks, run_has_owner=False, include_failed=True)
    ok &= _rec("I4b done 不在重排队集合", 1 not in rq and 1 in reuse, f"rq={rq} reuse={reuse}")
    ok &= _rec("I4c running/blocked 进入重排队", sorted(rq) == [2, 3], f"rq={rq}")
    return ok


def t_i5():
    print("\n=== I5 running：有主拒绝接管，无主才允许 ===")
    ok = _rec("I5a 无活进程 ⇒ requeue",
              policy_classify("running", run_has_owner=False) == "requeue")
    ok &= _rec("I5b 有活进程 ⇒ owned（拒绝）",
               policy_classify("running", run_has_owner=True) == "owned")
    return ok


def t_i6():
    print("\n=== I6 failed 默认跳过（可能副作用已落一半）===")
    ok = _rec("I6a 默认 skip", policy_classify("failed", include_failed=False) == "skip")
    ok &= _rec("I6b 显式开启才 requeue",
               policy_classify("failed", include_failed=True) == "requeue")
    ok &= _rec("I6c planned/ready/blocked/canceled 均 requeue",
               all(policy_classify(s) == "requeue"
                   for s in ("planned", "ready", "blocked", "canceled")))
    return ok


def t_i7():
    print("\n=== I7 apply_resume 只改状态，不清交付内容 ===")
    con = _db()
    ids = _seed(con, statuses=("done", "running", "blocked", "failed"))
    con.execute("UPDATE orch_checkpoints SET updated_at=datetime('now','-7200 seconds') WHERE run_id=901")
    con.commit()
    dec = prepare_resume(con, 901, stale_s=1800)
    ok = _rec("I7a 决策可执行", dec.get("ok") is True, f"reason={dec.get('reason')}")
    snapshot_before = {r["id"]: dict(r) for r in con.execute(
        "SELECT * FROM agent_tasks WHERE run_id=901").fetchall()}
    applied = apply_resume(con, dec)
    ok &= _rec("I7b 重排队数 = running+blocked = 2", applied == 2, f"applied={applied}")
    after = {r["id"]: dict(r) for r in con.execute(
        "SELECT * FROM agent_tasks WHERE run_id=901").fetchall()}
    ok &= _rec("I7c done 任务一字未动",
               all(snapshot_before[i] == after[i] for i in ids[:1]))
    ok &= _rec("I7d 被重排任务 status→ready 且 error 清空",
               after[ids[1]]["status"] == "ready" and not after[ids[1]]["error"]
               and after[ids[2]]["status"] == "ready" and not after[ids[2]]["error"])
    ok &= _rec("I7e **交付内容未被清空**（result/metadata/context 保留）",
               all((snapshot_before[i]["result"] or "") == (after[i]["result"] or "")
                   and (snapshot_before[i]["metadata"] or "") == (after[i]["metadata"] or "")
                   and (snapshot_before[i]["context"] or "") == (after[i]["context"] or "")
                   for i in ids))
    ok &= _rec("I7f failed 未被默认重排", after[ids[3]]["status"] == "failed")
    return ok


def t_i8():
    print("\n=== I8 prepare_resume 六道闸门逐个生效 ===")
    ok = True
    con = _db()
    d = prepare_resume(con, 555, stale_s=1800)
    ok &= _rec("I8a 无检查点 ⇒ 拒绝", d.get("ok") is False and d.get("reason") == "no_checkpoint")

    con2 = _db()
    _seed(con2, statuses=("planned",))
    con2.execute("UPDATE orch_checkpoints SET phase='done' WHERE run_id=901")
    con2.commit()
    d2 = prepare_resume(con2, 901, stale_s=1800)
    ok &= _rec("I8b 终态 phase ⇒ 拒绝", d2.get("ok") is False and d2.get("reason").startswith("phase_terminal"))

    con3 = _db()
    _seed(con3, statuses=("planned",))
    con3.execute("UPDATE orch_checkpoints SET attempt_count=9 WHERE run_id=901")
    con3.commit()
    d3 = prepare_resume(con3, 901, stale_s=1800)
    ok &= _rec("I8c 重试次数耗尽 ⇒ 拒绝", d3.get("ok") is False and d3.get("reason").startswith("attempt_exhausted"))

    con4 = _db()
    _seed(con4, statuses=("planned",))
    d4 = prepare_resume(con4, 901, stale_s=1800)
    ok &= _rec("I8d 心跳仍新鲜（运行中）⇒ 拒绝接管",
               d4.get("ok") is False and d4.get("reason") == "still_alive")

    con5 = _db()
    _seed(con5, statuses=())
    con5.execute("UPDATE orch_checkpoints SET updated_at=datetime('now','-7200 seconds')")
    con5.commit()
    d5 = prepare_resume(con5, 901, stale_s=1800)
    ok &= _rec("I8e 无任务行 ⇒ 拒绝", d5.get("ok") is False and d5.get("reason") == "no_tasks")

    con6 = _db()
    _seed(con6, statuses=("done",))
    con6.execute("UPDATE orch_checkpoints SET updated_at=datetime('now','-7200 seconds')")
    con6.commit()
    d6 = prepare_resume(con6, 901, stale_s=1800)
    ok &= _rec("I8f 全部已完成 ⇒ nothing_to_do（不得误判为可恢复）",
               d6.get("ok") is False and d6.get("reason") == "nothing_to_do")
    return ok


def t_i9():
    print("\n=== I9 端到端：崩溃现场 → resume 只补跑未完成的 ===")
    con = _db()
    ids = _seed(con, statuses=("done", "done", "running", "blocked"))
    con.execute("UPDATE orch_checkpoints SET updated_at=datetime('now','-5400 seconds')")
    con.commit()
    items = CK["resumable"](con, conversation_id=77, limit=10, stale_s=1800)
    ok = _rec("I9a 该批次出现在可恢复清单", len(items) == 1 and items[0]["run_id"] == 901,
              f"items={items}")
    ok &= _rec("I9b 清单给出待执行数 = 2", items and items[0]["pending"] == 2, f"{items}")
    dec = prepare_resume(con, 901, stale_s=1800)
    ok &= _rec("I9c 通过全部闸门", dec.get("ok") is True, f"reason={dec.get('reason')}")
    ok &= _rec("I9d 复用 2 个已完成 + 重排 2 个未完成",
               len(dec.get("reuse_ids") or []) == 2 and len(dec.get("requeue_ids") or []) == 2,
               f"reuse={dec.get('reuse_ids')} rq={dec.get('requeue_ids')}")
    apply_resume(con, dec)
    rows = {r["task_key"]: r["status"] for r in con.execute(
        "SELECT task_key, status FROM agent_tasks WHERE run_id=901").fetchall()}
    ok &= _rec("I9e 完成后 t1/t2 仍 done、t3/t4 转为 ready",
               rows == {"t1": "done", "t2": "done", "t3": "ready", "t4": "ready"}, f"{rows}")
    attempt = con.execute("SELECT attempt_count FROM orch_checkpoints WHERE run_id=901").fetchone()[0]
    _ = CK["bump_attempt"](con, 901)
    a2 = con.execute("SELECT attempt_count FROM orch_checkpoints WHERE run_id=901").fetchone()[0]
    ok &= _rec("I9f 每次 resume 计数 +1（死循环护栏）", a2 == int(attempt or 0) + 1, f"{attempt}->{a2}")
    return ok


def t_i10():
    print("\n=== I10 接线契约：resume 下跳过 planner 与 create_plan ===")
    src = open(STREAM_SRC, encoding="utf-8").read()
    ok = _rec("I10a execute_stream 接受 resume_run_id",
              "resume_run_id: int = 0" in src)
    ok &= _rec("I10b resume 直达通道存在（绕过意图识别后的启发式判定）",
               "if resume_run_id and not dry_run:" in src)
    # 关键：planner LLM 调用必须落在 `if not _resume_ck:` 守卫下
    idx_guard = src.find("if not _resume_ck:")
    idx_plan = src.find("_intent=\"planner\"")
    ok &= _rec("I10c planner 调用位于 `if not _resume_ck` 守卫之后",
               idx_guard > 0 and idx_plan > idx_guard > 0,
               f"guard={idx_guard} planner={idx_plan}")
    idx_create = src.find("TaskQueue.create_plan(")
    ok &= _rec("I10d create_plan 位于 `if _resume_ck:` 的 else 分支内",
               idx_create > src.find("if _resume_ck:") > 0 and idx_create > idx_guard)
    ok &= _rec("I10e 心跳接入主循环（含节流）",
               "_ck_hb(conn, run_id, None, every_s=5.0)" in src)
    ok &= _rec("I10f 汇总/收尾 phase 推进已接",
               '_ck_sm(conn, run_id, "summarizing")' in src and '_ck_fin(conn, run_id, "done")' in src)
    return ok


def t_i11():
    print("\n=== I11 时区口径：时间比较一律在 SQL 内 ===")
    src = open(CK_SRC, encoding="utf-8").read()
    ok = _rec("I11a julianday 差值在 SQL 内完成", "julianday('now') - julianday(updated_at)" in src)
    ok &= _rec("I11b 无 Python 侧解析时间戳后相减的写法",
               "strptime" not in src and "datetime(" not in src.replace("datetime('now'", ""))
    ok &= _rec("I11c 文件头已写明 UTC vs localtime 坑", "CURRENT_TIMESTAMP` 是 **UTC**" in src)
    return ok


def t_i12():
    print("\n=== I12 配置段与建表契约 ===")
    from core import config as _cfg
    ok = _rec("I12a orchestration 配置段存在",
              isinstance(_cfg.get("orchestration", "resume_stale_s", None), int))
    ok &= _rec("I12b 默认不做自动重投（安全的默认值）",
               _cfg.get("orchestration", "auto_resume_on_startup", None) is False)
    ok &= _rec("I12c failed 默认不自动重跑",
               _cfg.get("orchestration", "resume_include_failed", None) is False)
    schema = open(os.path.join(ROOT, "database", "schema.py"), encoding="utf-8").read()
    ok &= _rec("I12d schema 含 orch_checkpoints 建表",
               "CREATE TABLE IF NOT EXISTS orch_checkpoints" in schema)
    ok &= _rec("I12e run_id 唯一索引（UPSERT 前提）",
               "CREATE UNIQUE INDEX IF NOT EXISTS ux_ock_run" in schema)
    # 真跑一次 init_db：新库必须能建出该表（含迁移幂等）。
    # ⚠️ 必须**直接改模块的 DB_PATH**，不能靠环境变量 —— `database.schema` 在 import 期就把
    #    `core.config.DB_PATH` 绑进了本模块作用域，验证脚本顶部已 import 过 core ⇒
    #    设置 env 晚了，init_db 会直接迁移**生产库**（本脚本首跑就踩了这条，结果在真实库上
    #    建了一张空表，虽然无害，但"验证脚本写生产库"是不可接受的）。故这里走 monkeypatch。
    import tempfile
    p = os.path.join(tempfile.gettempdir(), "_ck_init_%d.db" % os.getpid())
    if os.path.exists(p):
        os.remove(p)
    try:
        from database import schema as _schema, connection as _conn_mod
        _prev_schema, _prev_conn = _schema.DB_PATH, getattr(_conn_mod, "DB_PATH", None)
        _schema.DB_PATH = p
        if _prev_conn is not None:
            _conn_mod.DB_PATH = p
        try:
            _schema.init_db()
        finally:
            _schema.DB_PATH = _prev_schema
            if _prev_conn is not None:
                _conn_mod.DB_PATH = _prev_conn
        con = sqlite3.connect(p)
        n = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                        "AND name='orch_checkpoints'").fetchone()[0]
        con.close()
        ok &= _rec("I12f init_db 在新库上确实建出该表", n == 1, f"n={n}")
    finally:
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return ok


def t_i13():
    """I13 「汇总阶段崩溃」的恢复语义 —— 用**真实 TaskQueue** 证明。

    推断很容易说错，所以这里不靠读代码下结论：直接问 TaskQueue ——
    若全部任务已 done，则 `pending_count`=0 ⇒ `_stream_orchestrated_flow` 的主 while
    **一次都不进**，直接落到汇总段。这正是"崩在汇总时不必重跑子任务"的依据。
    """
    print("\n=== I13 汇总阶段崩溃：只重做汇总，不重跑任务 ===")
    from task_queue import TaskQueue
    con = _db()
    _seed(con, statuses=("done", "done", "done"))
    ok = _rec("I13a 全部 done ⇒ pending_count=0（主循环直接跳过）",
              TaskQueue.pending_count(con, 901) == 0,
              f"got={TaskQueue.pending_count(con, 901)}")
    ready = TaskQueue.ready_tasks(con, 901)
    ok &= _rec("I13b ready_tasks 为空（不会挑出任何任务执行）", ready == [], f"got={len(ready)}")
    con2 = _db()
    _seed(con2, statuses=("done", "ready"))
    ok &= _rec("I13c 有未完成任务时确实 >0（对照组，防计数器恒 0 的空转）",
               TaskQueue.pending_count(con2, 901) > 0 and len(TaskQueue.ready_tasks(con2, 901)) == 1)
    # 当前实现仍拒绝"全 done"批次的 resume（理由下一行），把缺口固化成断言而不是假装不存在
    d = prepare_resume(con, 901, stale_s=0)
    ok &= _rec("I13d 已知缺口：全 done 批次被判 nothing_to_do（见报告 §已知缺口）",
               d.get("reason") == "nothing_to_do", f"reason={d.get('reason')}")
    return ok


def t_i14():
    """C-1 汇总阶段崩溃 → 只重做汇总，不重跑子任务。"""
    print("\n=== C-1 汇总阶段断点恢复（省掉整轮任务 LLM 费用）===")
    summary_only_eligible = CK["summary_only_eligible"]
    mark_summary_written = CK["mark_summary_written"]

    # ① 崩在汇总：子任务全 done、消息未落库 ⇒ 应可只重做汇总
    con = _db()
    _seed(con, statuses=("done", "done", "done"))
    con.execute("UPDATE orch_checkpoints SET phase='summarizing', "
                "updated_at=datetime('now','-7200 seconds') WHERE run_id=901")
    con.commit()
    ok, why = summary_only_eligible(con, 901)
    ok_ok = _rec("C-1a 汇总阶段且未落消息 ⇒ 可只重做汇总", ok is True and why == "ok", f"why={why}")
    dec = prepare_resume(con, 901, stale_s=1800)
    ok_ok &= _rec("C-1b prepare_resume 放行且 mode=summary",
                  dec.get("ok") is True and dec.get("mode") == "summary",
                  f"ok={dec.get('ok')} mode={dec.get('mode')} reason={dec.get('reason')}")
    ok_ok &= _rec("C-1c **不重排任何任务**（这是省钱的要害）",
                  (dec.get("requeue_ids") or []) == [], f"rq={dec.get('requeue_ids')}")
    ok_ok &= _rec("C-1d 全部已完成任务被列为复用",
                  len(dec.get("reuse_ids") or []) == 3, f"reuse={dec.get('reuse_ids')}")

    # ② 已落消息 ⇒ 必须拒绝（否则同一份汇总在会话里出现两次）
    con2 = _db()
    _seed(con2, statuses=("done", "done", "done"))
    con2.execute("UPDATE orch_checkpoints SET phase='summarizing' WHERE run_id=901")
    mark_summary_written(con2, 901)
    con2.commit()
    ok2, why2 = summary_only_eligible(con2, 901)
    ok_ok &= _rec("C-1e 已落消息(标记=1) ⇒ 拒绝重做", ok2 is False and why2 == "summary_already_written",
                  f"why={why2}")
    d2 = prepare_resume(con2, 901, stale_s=1800)
    ok_ok &= _rec("C-1f 已落消息 ⇒ prepare_resume 拒绝", d2.get("ok") is False, f"ok={d2.get('ok')}")

    # ③ **冗余兜底**：标记丢了但消息确实在（模拟有人把 mark 挪出事务）⇒ 仍须拒绝
    con3 = _db()
    _seed(con3, statuses=("done", "done"))
    con3.execute("UPDATE orch_checkpoints SET phase='summarizing' WHERE run_id=901")
    con3.execute("INSERT INTO messages (conversation_id, role, content) VALUES (77,'assistant','汇总正文')")
    con3.commit()
    ok3, why3 = summary_only_eligible(con3, 901)
    ok_ok &= _rec("C-1g 标记丢失但消息存在 ⇒ 仍拒绝（最后一道闸）",
                  ok3 is False and why3.startswith("assistant_message_exists"), f"why={why3}")

    # ④ 非汇总阶段 ⇒ 不得走 summary 通道
    con4 = _db()
    _seed(con4, statuses=("done", "done"))
    con4.execute("UPDATE orch_checkpoints SET phase='executing' WHERE run_id=901")
    con4.commit()
    ok4, why4 = summary_only_eligible(con4, 901)
    ok_ok &= _rec("C-1h executing 阶段不走 summary 通道",
                  ok4 is False and why4.startswith("phase_not_summarizing"), f"why={why4}")

    # ⑤ 可恢复清单要能列出"仅汇总"型批次（pending=0 但仍有恢复价值）
    con5 = _db()
    _seed(con5, statuses=("done", "done"))
    con5.execute("UPDATE orch_checkpoints SET phase='summarizing', "
                 "updated_at=datetime('now','-7200 seconds') WHERE run_id=901")
    con5.commit()
    items = CK["resumable"](con, conversation_id=0, limit=5, stale_s=1800)
    it5 = CK["resumable"](con5, conversation_id=0, limit=5, stale_s=1800)
    ok_ok &= _rec("C-1i 清单收录仅汇总型批次并标注 summary_only",
                  len(it5) == 1 and it5[0].get("summary_only") is True, f"items={it5}")

    # ⑥ **回归**：会话里已有历史 assistant 消息（编排之前）⇒ 不得因此堵死 summary 通道。
    #    这条是端到端验证抓出来的真缺陷：第一版按会话粒度查"有没有 assistant 消息"，
    #    而任何走到编排阶段的会话都已有历史消息 ⇒ 本功能**永久失效**却看不出来。
    con6 = _db()
    _seed(con6, statuses=("done", "done"))
    con6.execute("UPDATE orch_checkpoints SET phase='summarizing', "
                 "created_at=datetime('now','-1 hour') WHERE run_id=901")
    con6.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                 "VALUES (77,'assistant','三天前的旧回答', datetime('now','-2 hour'))")
    con6.commit()
    ok6, why6 = summary_only_eligible(con6, 901)
    ok_ok &= _rec("C-1j **编排之前**的历史消息不阻断恢复（第一版的真缺陷回归）",
                  ok6 is True, f"ok={ok6} why={why6}")
    # 编排**之后**的消息则必须阻断（这才是真正的重复风险）
    con7 = _db()
    _seed(con7, statuses=("done", "done"))
    con7.execute("UPDATE orch_checkpoints SET phase='summarizing', "
                 "created_at=datetime('now','-1 hour') WHERE run_id=901")
    con7.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                 "VALUES (77,'assistant','崩溃前刚落库的汇总', datetime('now','-30 minute'))")
    con7.commit()
    ok7, why7 = summary_only_eligible(con7, 901)
    ok_ok &= _rec("C-1k 编排之后的消息仍阻断（防重复汇总）",
                  ok7 is False and why7.startswith("assistant_message_exists"), f"why={why7}")
    return ok_ok


def t_i15():
    """C-1 顺带修掉的缺陷：resume 不得插空 user 消息。"""
    print("\n=== C-1b resume 不得产生新一轮 user 消息 ===")
    src = open(STREAM_SRC, encoding="utf-8").read()
    idx = src.find('if not dry_run and not resume_run_id:')
    ok = _rec("C-1b1 入口 user 消息落库已排除 resume 路径", idx > 0, f"idx={idx}")
    ok &= _rec("C-1b2 条件里同时含 dry_run 与 resume_run_id",
               idx > 0 and "dry_run and not resume_run_id" in src[idx:idx + 60])
    # 同事务判据：_ck_msw 调用必须落在 `with db_conn() as conn2:` 块内（块尾 = conn.close() 之前）
    i_msw = src.find("_ck_msw(conn2, run_id)")
    i_open = src.find("with db_conn() as conn2:")
    i_close = src.find("        try:\n            conn.close()")
    ok &= _rec("C-1b3 summary_written 标记写在消息 INSERT 的同一事务块内",
               i_msw > 0 and i_open > 0 and i_close > i_msw > i_open,
               f"open={i_open} msw={i_msw} close={i_close}")
    return ok


def _mk(build_fn):
    """构造一个变异体（返回新命名空间）。"""
    return build_fn()


# ── 变异自证：注入"错写法"，对应不变式必须判 FAIL ─────────────────────────────
def m_i4():
    """M1（最危险）：让 done 任务也被重跑 —— 会造成**不可逆的重复写实体**。"""
    print("\n=== 变异 M1：把 done 也纳入重排队 ===")
    ns = _reload(('if st == "done":\n        return "reuse"',
                  'if st == "done":\n        return "requeue"'))
    v = ns["policy_classify"]("done", include_failed=True)
    tasks = [{"id": 1, "task_key": "t1", "status": "done"}]
    rq, _, _ = ns["pick_requeue_ids"](tasks, include_failed=True)
    caught = (v != "reuse") and (1 in rq)
    return _rec("M1 变异被复现 → 证明 I4 非空转", caught, f"v={v} rq={rq}", VACUOUS)


def m_i5():
    """M2：去掉"有活进程不得接管"的闸门 —— 两进程并发执行同一任务。"""
    print("\n=== 变异 M2：running 一律允许接管（忽略是否有主）===")
    ns = _reload(('if st == "running":\n        return "owned" if run_has_owner else "requeue"',
                  'if st == "running":\n        return "requeue"'))
    caught = ns["policy_classify"]("running", run_has_owner=True) == "requeue"
    return _rec("M2 变异被复现 → 证明 I5 非空转", caught, "", VACUOUS)


def m_i7():
    """M3：apply_resume 顺手清掉 result —— 已交付内容被抹掉。"""
    print("\n=== 变异 M3：apply_resume 连带清空 result ===")
    ns = _reload(("SET status='ready', error='', updated_at=CURRENT_TIMESTAMP",
                  "SET status='ready', error='', result='', updated_at=CURRENT_TIMESTAMP"))
    con = _db()
    ids = _seed(con, statuses=("done", "running"))
    # 现实中崩溃往往发生在"已写出部分结果"之后：running 任务带着半截 result 更符合现场，
    # 也只有这样才能观测到"重排队时 result 被抹掉"这个伤害（否则该列本就是空，变异不可见）。
    con.execute("UPDATE agent_tasks SET result='半截结果', metadata='{\"partial\":true}' "
                "WHERE status='running' AND run_id=901")
    con.execute("UPDATE orch_checkpoints SET updated_at=datetime('now','-7200 seconds')")
    con.commit()
    before = {r["id"]: r["result"] for r in con.execute(
        "SELECT id, result FROM agent_tasks WHERE run_id=901").fetchall()}
    dec = ns["prepare_resume"](con, 901, stale_s=1800)
    ns["apply_resume"](con, dec)
    after = {r["id"]: r["result"] for r in con.execute(
        "SELECT id, result FROM agent_tasks WHERE run_id=901").fetchall()}
    cleared = [i for i in ids if (before[i] or "") and not (after[i] or "")]
    return _rec("M3 变异被复现（结果被清空）→ 证明 I7e 非空转",
                len(cleared) > 0, f"cleared={cleared}", VACUOUS)


def m_i8():
    """M4：去掉存活闸门 —— 会接管正在运行的批次（双跑）。"""
    print("\n=== 变异 M4：prepare_resume 不做存活判定 ===")
    ns = _reload(('    if is_alive(conn, run_id, stale_s):',
                  '    if False:'))
    con = _db()
    _seed(con, statuses=("planned",))
    d = ns["prepare_resume"](con, 901, stale_s=1800)
    caught = d.get("ok") is True and d.get("reason") == "ok"
    return _rec("M4 变异被复现（运行中批次被放行）→ 证明 I3/I8d 非空转",
                caught, f"reason={d.get('reason')}", VACUOUS)


def m_i2():
    """M5：心跳不节流 —— 主循环每 50 ms 转一圈会瞬间打满 SQLite 单写者。"""
    print("\n=== 变异 M5：忽略 every_s 节流参数 ===")
    ns = _reload(("            if every_s and every_s > 0:",
                  "            if False:"))
    con = _db()
    _seed(con, statuses=("planned",))
    before = con.execute("SELECT updated_at FROM orch_checkpoints WHERE run_id=901").fetchone()[0]
    time.sleep(1.05)
    for _ in range(5):
        ns["touch"](con, 901, None, every_s=30.0)
    after = con.execute("SELECT updated_at FROM orch_checkpoints WHERE run_id=901").fetchone()[0]
    return _rec("M5 变异被复现（节流失效，时间戳被刷新）→ 证明 I2a 非空转",
                before != after, f"{before} -> {after}", VACUOUS)


def m_c1():
    """M6：去掉"已落消息就拒绝"这条闸门 —— 恢复会在会话里产生**重复的汇总消息**。"""
    print("\n=== 变异 M6：summary 通道不查消息是否已落库 ===")
    ns = _reload(('    if int(ck.get("summary_written") or 0):\n        return False, "summary_already_written"',
                  '    if False:\n        return False, "summary_already_written"'))
    con = _db()
    _seed(con, statuses=("done", "done"))
    con.execute("UPDATE orch_checkpoints SET phase='summarizing' WHERE run_id=901")
    ns["mark_summary_written"](con, 901)
    con.commit()
    ok, why = ns["summary_only_eligible"](con, 901)
    # 若仅靠标记拦住，变异后应变成"可重做"（= 危险）；靠消息 JOIN 才能继续拦住
    caught = (ok is True) or (why == "probe_failed")
    return _rec("M6 变异被复现（已落消息仍放行）→ 证明 C-1e/f 非空转",
                caught, f"ok={ok} why={why}", VACUOUS)


def main():
    print("=" * 72)
    print("P0-2 第二步：编排 checkpoint + 断点续跑 —— 不变式与变异自证")
    print("=" * 72)
    t_i1(); t_i2(); t_i3(); t_i4(); t_i5(); t_i6(); t_i7()
    t_i8(); t_i9(); t_i10(); t_i11(); t_i12(); t_i13(); t_i14(); t_i15()
    m_i4(); m_i5(); m_i7(); m_i8(); m_i2(); m_c1()

    bad = [r for r in _results if r[0] != PASS]
    vac = [r for r in _results if r[0] == VACUOUS]
    print("\n" + "=" * 72)
    print(f"断言总数 {len(_results)}  PASS {len(_results) - len(bad)}  FAIL/VACUOUS {len(bad)}"
          f"（其中 VACUOUS {len(vac)}）")
    if bad:
        for k, n, d in bad:
            print(f"  {k}  {n}  {d}")
        print("\n结论：有断言未通过 / 至少一组变异未被复现（空转）")
        return 1
    print("结论：全部通过，且 6 组变异均被复现（断言非空转）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
