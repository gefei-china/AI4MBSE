"""P0-2 持久执行：会话编排 run 的检查点（checkpoint）与"断点续跑"（resume）策略。

━━━ 为什么需要这张表 ━━━
会话编排（``agent/pipeline_parts/stream.py::_stream_orchestrated_flow``）的执行上下文全在
**函数栈 + 线程**里：计划、已收集的子任务事件、provider、分支都只活在局部变量。
进程一崩（重启 / 客户端断连后 finally 清栈 / worker 异常），这些就全部消失 ——
`agent_tasks` 里虽然还留着每个子任务的行，但**没人知道**这批任务原本要跑什么、
用哪个模型、跑到哪一阶段，于是只能整批标记 failed（批次 1 已实现的孤儿回收）。

对标 LangGraph：`checkpointer` 存的是 (thread_id, checkpoint_ns) → state；
这里 `agent_tasks` 已经是逐行 commit 的 state store，所以本模块**只补"重进这张图的坐标系"**：
    阶段 phase + 原始输入 + 意图 + 分支 + provider + 原始计划 + 心跳时间戳。
刻意**不**做整图状态快照 —— 那会引入双份账本、且必然漂移（状态真源只有 agent_tasks 一处）。

━━━ resume 的安全口径（最重要的一段）━━━
resume 不是"重跑一遍"，而是"只补跑没做完的"：
  · done           → 一律复用结果，**绝不重跑**（幂等的根本）
  · planned/ready  → 重排队
  · blocked/canceled → 重排队
  · running        → 只有当**确认无活进程**（心跳超时）才允许接管，否则拒绝（防双跑写脏数据）
  · failed         → 默认**不**自动重跑（它可能已经把副作用写了一半），需显式 `include_failed=True`
这条口径看着保守，是因为子任务会调 `entity_create` 等写工具：盲目重跑 = 重复实体。

━━━ 时区坑（实测）━━━
SQLite 的 `CURRENT_TIMESTAMP` 是 **UTC**，Python 的 `datetime.now()` 是**本地时间**（本机 UTC+8）。
两者相减会凭空多出 8 小时 → 心跳判定可能"永远活着"或"永远超时"。
所以本模块**所有时间比较一律在 SQL 内用 julianday 完成**，不把时间戳取回 Python 做差。
"""
import json

__all__ = [
    "PHASE_PLANNED", "PHASE_EXECUTING", "PHASE_SUMMARIZING", "PHASE_DONE",
    "PHASE_FAILED", "PHASE_INTERRUPTED",
    "ALIVE_STATUSES", "DEFAULT_STALE_S",
    "save", "load", "touch", "finish", "bump_attempt", "mark_interrupted",
    "mark_summary_written", "summary_only_eligible",
    "is_alive", "stale_seconds", "resumable", "policy_classify",
    "pick_requeue_ids", "prepare_resume", "apply_resume",
]

PHASE_PLANNED = "planned"
PHASE_EXECUTING = "executing"
PHASE_SUMMARIZING = "summarizing"
PHASE_DONE = "done"
PHASE_FAILED = "failed"
PHASE_INTERRUPTED = "interrupted"     # 被孤儿回收标记为已失效（批次 1 `core/run_registry`）

TERMINAL_PHASES = (PHASE_DONE, PHASE_FAILED)

# 可被接管（resume）的任务状态 —— 见文件头"resume 的安全口径"
ALIVE_STATUSES = ("planned", "ready", "running", "blocked")
REQUEUE_STATUSES = ("planned", "ready", "blocked", "canceled")

DEFAULT_STALE_S = 1800      # 30 分钟无心跳即判定失活（编排单任务实测上限 1155 s，取 1.5 倍余量）
_MAX_ATTEMPTS = 5           # resume 次数上限：超过即不再自愈（防"恢复→再崩→再恢复"死循环）

# ── 可续跑的失败类别（P0-1，2026-10-30）──────────────────────────────────
# ★ 这条常量解决的是本工程**最大的失败源**：
#   真库实测 failed 84 条里 `error_class='interrupted'` 占 20 条（24%），
#   而近 4 天近乎 100% —— 它们的成因是**宿主没了**（进程重启 / 执行线程丢失），
#   不是"任务做错了"。旧口径用 `include_failed=False` 把它们一刀切 skip ⇒
#   **结构上永远等不到自愈**，成功率被钉死在 70% 量级。
#
# ★ 为什么"重跑 interrupted"与"重跑失败任务"是两件事（不该共用 include_failed）：
#   · `include_failed=True` 的顾虑是"副作用可能已写了一半 ⇒ 重跑产生重复实体"；
#   · 而 interrupted 的重跑语义**等价于续跑**（同一个 run、同一个 task_key），
#     只要写类工具幂等开启，重复写会被**重放**而不是再执行一次
#     （`core/idempotency.py`，键 = run|task|args|ordinal，重试时序号对齐）。
#   ⇒ 于是"能不能重跑"的判据从"状态是否为 failed"细化为"**这个 failed 是不是进程死亡**"。
#
# ⚠️ 白名单而非黑名单：只有明确知道"重跑安全"的类才放行。
#   timeout（逻辑停滞）/ internal / 未知 一律仍按 include_failed 处理 ——
#   它们可能是任务本身的确定性缺陷，重跑只会再烧一次额度。
SAFE_REQUEUE_CLASSES = ("interrupted",)


def safe_requeue_classes() -> tuple:
    """当前可"自动续跑"的失败类别（受两道开关约束，缺一即返回空）。

    守卫①：`orchestration.resume_interrupted`（默认 True）——运维可一键回到旧行为。
    守卫②：**写类工具幂等必须开着**。幂等关了还重跑 interrupted，
           就是把"重复写"的风险重新引进来；此时宁可继续 skip（等人点"继续编排"）。

    返回空元组 ⇒ 调用方行为与改动前**逐字节一致**（零回归路径）。
    """
    try:
        from core import config as _c
        if not bool(_c.get("orchestration", "resume_interrupted", True)):
            return ()
    except Exception:
        pass
    try:
        from core import idempotency as _idem
        if not _idem.enabled():
            return ()
    except Exception:
        return ()            # 判不了 ⇒ 保守不放行
    return tuple(SAFE_REQUEUE_CLASSES)


# ── 写入 ────────────────────────────────────────────────────────────────────
def save(conn, run_id: int, *, conversation_id: int = 0, phase: str = PHASE_PLANNED,
         user_input: str = "", intent: str = "", branch: str = "", provider_id=0,
         skill_name: str = "", team: str = "", plan=None, params: dict | None = None) -> bool:
    """落一次检查点（首次 INSERT，重复调用则 UPDATE 其余字段并保持首建时间）。

    失败静默：**checkpoint 是旁路设施，绝不能成为编排的故障源**
    （同批次 1 限流中间件的取信原则）。返回是否成功写入。
    """
    if not run_id:
        return False
    try:
        _pj = json.dumps(plan or [], ensure_ascii=False)[:60000]
        _prj = json.dumps(params or {}, ensure_ascii=False)[:20000]
        conn.execute(
            "INSERT INTO orch_checkpoints (run_id, conversation_id, phase, user_input, intent, "
            "  branch, provider_id, skill_name, team, plan_json, params_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "  conversation_id=excluded.conversation_id, phase=excluded.phase, "
            "  user_input=CASE WHEN excluded.user_input<>'' THEN excluded.user_input ELSE user_input END, "
            "  intent=CASE WHEN excluded.intent<>'' THEN excluded.intent ELSE intent END, "
            "  branch=CASE WHEN excluded.branch<>'' THEN excluded.branch ELSE branch END, "
            "  provider_id=CASE WHEN excluded.provider_id>0 THEN excluded.provider_id ELSE provider_id END, "
            "  skill_name=excluded.skill_name, team=excluded.team, "
            "  plan_json=CASE WHEN excluded.plan_json<>'[]' THEN excluded.plan_json ELSE plan_json END, "
            "  params_json=excluded.params_json, updated_at=CURRENT_TIMESTAMP",
            (int(run_id), int(conversation_id or 0), phase or PHASE_PLANNED,
             (user_input or "")[:8000], (intent or "")[:200], (branch or "")[:200],
             int(provider_id or 0), (skill_name or "")[:200], (team or "")[:200], _pj, _prj))
        conn.commit()
        return True
    except Exception:
        # 表不存在（老库未跑 init_db）/ 写冲突 → 都不允许影响编排主链路
        return False


def touch(conn, run_id: int, phase: str | None = None, *, every_s: float = 0.0) -> None:
    """心跳：刷新 `updated_at`（+ 可选改 phase）。

    `every_s > 0` 时做**节流**：距上次刷新不足该秒数则跳过，避免主循环每 50 ms 一次 UPDATE
    把 SQLite 的单写者瓶颈打满（这是真实风险：本库是单写者，编排本身已在并发写任务行）。
    """
    if not run_id:
        return
    try:
        if phase:
            conn.execute(
                "UPDATE orch_checkpoints SET phase=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (phase, int(run_id)))
        else:
            q = "UPDATE orch_checkpoints SET updated_at=CURRENT_TIMESTAMP WHERE run_id=?"
            args = (int(run_id),)
            if every_s and every_s > 0:
                # 节流判据在 SQL 内完成（UTC vs 本地时间的坑见文件头）
                q += " AND (julianday('now') - julianday(updated_at)) * 86400.0 >= ?"
                args = (int(run_id), float(every_s))
            conn.execute(q, args)
        conn.commit()
    except Exception:
        pass


def finish(conn, run_id: int, phase: str) -> None:
    """终态收尾（done / failed）。"""
    touch(conn, run_id, phase)


def mark_interrupted(conn, run_ids) -> int:
    """批次挂钩：把被孤儿回收判定失效的 run 标记 `interrupted`（供 UI 与 resume 判定）。

    ⚠️ **刻意不刷新 `updated_at`**（2026-10-30，门禁 C4~C7 抓出来的真 bug）：
       `is_alive()` 的判据是"心跳是否还在 stale 窗口内"，而本函数的语义是
       "这个批次**已经没有进展了**"。第一版写成 `updated_at=CURRENT_TIMESTAMP`，
       等于**用'标记失活'这个动作刷新了失活判据本身** ⇒ 标记完立刻 `still_alive`，
       批次永远既不可续跑、又不算死 —— 比不标更糟（僵尸从"看不见"变成"看得见但动不了"）。
       语义上 `updated_at` 是**进展**时间戳，宣布失败不是进展。
    ⚠️ 只改非终态批次：已 done/failed 的批次不该被"回收"改写阶段（防覆盖真实结论）。
    """
    ids = [int(x) for x in (run_ids or []) if int(x or 0) > 0]
    if not ids:
        return 0
    try:
        q = ("UPDATE orch_checkpoints SET phase=? "
             "WHERE run_id IN (%s) AND phase NOT IN ('done','failed')" % ",".join("?" * len(ids)))
        conn.execute(q, tuple([PHASE_INTERRUPTED]) + tuple(ids))
        conn.commit()
        return len(ids)
    except Exception:
        return 0


def bump_attempt(conn, run_id: int) -> int:
    """resume 计数 +1，返回最新值。达到上限后 `resumable` 不再给出该 run（防自愈死循环）。"""
    try:
        conn.execute(
            "UPDATE orch_checkpoints SET attempt_count=COALESCE(attempt_count,0)+1, "
            "updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (int(run_id),))
        conn.commit()
        row = conn.execute("SELECT attempt_count FROM orch_checkpoints WHERE run_id=?",
                           (int(run_id),)).fetchone()
        return int((row[0] if row else 0) or 0)
    except Exception:
        return 0


# ── 读取 ────────────────────────────────────────────────────────────────────
def load(conn, run_id: int) -> dict | None:
    """读一个检查点；无则 None（含表不存在的情形）。"""
    if not run_id:
        return None
    try:
        cur = conn.execute("SELECT * FROM orch_checkpoints WHERE run_id=?", (int(run_id),))
        row = cur.fetchone()
        if row is None:
            return None
        if hasattr(row, "keys"):
            d = dict(row)
        else:
            names = [c[0] for c in cur.description]
            d = dict(zip(names, row))
    except Exception:
        return None
    for k in ("plan_json", "params_json"):
        raw = d.get(k) or ""
        try:
            d[k[:-5]] = json.loads(raw) if raw else ([] if k == "plan_json" else {})
        except Exception:
            d[k[:-5]] = [] if k == "plan_json" else {}
    return d


def stale_seconds(conn, run_id: int) -> float:
    """距上次心跳多少秒（SQL 内计算）。查不到返回 -1。"""
    try:
        row = conn.execute(
            "SELECT (julianday('now') - julianday(updated_at)) * 86400.0 FROM orch_checkpoints "
            "WHERE run_id=?", (int(run_id),)).fetchone()
        return float(row[0]) if row and row[0] is not None else -1.0
    except Exception:
        return -1.0


def is_alive(conn, run_id: int, stale_s: int | None = None) -> bool:
    """该 run 是否**仍被某个活进程持有**。

    判据：① phase 非终态 且 ② 心跳未超时。心跳用 SQL 时间差算（不在 Python 做减法，见文件头时区坑）。
    """
    ck = load(conn, run_id)
    if not ck:
        return False
    _ph = ck.get("phase") or ""
    if _ph in TERMINAL_PHASES:
        return False
    # ★ 2026-10-30：`interrupted` 的定义就是"没有活着的宿主"——
    #   不该再走到心跳比较（那条判据依赖 `updated_at`，而标记失活本身不应改动它）。
    #   两处判据都留着是刻意的：即便日后有人又给 `mark_interrupted` 加了 `updated_at`，
    #   这里也不会再出现"宣布死亡却仍判活着"的自相矛盾。
    if _ph == PHASE_INTERRUPTED:
        return False
    s = stale_seconds(conn, run_id)
    if s < 0:
        return False
    return s < float(stale_s if stale_s is not None else DEFAULT_STALE_S)


# ── resume 策略（纯函数，便于脚本抽取验证）─────────────────────────────────
def policy_classify(status: str, *, run_has_owner: bool = False,
                    include_failed: bool = False, error_class: str = "",
                    requeue_classes=()) -> str:
    """单个任务状态 → 处置：`reuse` | `requeue` | `skip` | `owned`。

    run_has_owner=True 表示这个 run 还有活进程在跑 —— 此时 running 任务绝不能被接管
    （否则两个进程同时执行同一任务，写类工具会产生重复实体）。

    `error_class` + `requeue_classes`（P0-1，2026-10-30）：把 `failed` 再拆一层 ——
    `error_class ∈ requeue_classes` 的是**进程死亡**类失败（宿主没了，不是任务做错了），
    它的重跑等价于续跑，不该被 `include_failed=False` 一刀切挡掉。
    详见 `safe_requeue_classes()` 的注释（含为什么这条与幂等键是配套的）。
    """
    st = (status or "").strip()
    if st == "done":
        return "reuse"                      # 幂等的根本：已完成的一律不重跑
    if st == "running":
        return "owned" if run_has_owner else "requeue"
    if st == "failed":
        if include_failed:
            return "requeue"
        # ★ 进程死亡类失败：放行（白名单 + 幂等双守卫，见模块常量注释）
        if str(error_class or "").strip() in tuple(requeue_classes or ()):
            return "requeue"
        return "skip"
    if st in REQUEUE_STATUSES:
        return "requeue"
    return "skip"


def pick_requeue_ids(tasks: list, *, run_has_owner: bool = False,
                     include_failed: bool = False, requeue_classes=()) -> tuple:
    """任务行列表 → (待重排队 id 列表, 复用 id 列表, 跳过 id 列表)。

    `tasks`: [{id, task_key, status, error_class, ...}]
    """
    requeue, reuse, skip = [], [], []
    for t in tasks or []:
        verdict = policy_classify(t.get("status") or "", run_has_owner=run_has_owner,
                                 include_failed=include_failed,
                                 error_class=str(t.get("error_class") or ""),
                                 requeue_classes=requeue_classes)
        tid = t.get("id")
        if verdict == "requeue":
            requeue.append(tid)
        elif verdict == "reuse":
            reuse.append(tid)
        else:
            skip.append((tid, verdict))
    return requeue, reuse, skip


def prepare_resume(conn, run_id: int, *, include_failed: bool = False,
                   stale_s: int | None = None, max_attempts: int = _MAX_ATTEMPTS,
                   requeue_classes=None) -> dict:
    """为一次 resume 做全部前置判定，**返回决策而不落任何写操作**（便于测试与灰度）。

    :returns: {"ok": bool, "reason": str, "mode": "tasks"|"summary"|"", "checkpoint": dict|None,
               "requeue_ids": [...], "reuse_ids": [...], "skip": [...],
               "attempt": int, "stale_s": float}
    `mode="summary"` 表示**只重做汇总**（子任务全部完成、只是汇总崩了）——
    这是 C-1 的价值：不重跑任何子任务，省掉整轮任务 LLM 费用。
    """
    ck = load(conn, run_id)
    if not ck:
        return {"ok": False, "reason": "no_checkpoint", "mode": "", "checkpoint": None,
                "requeue_ids": [], "reuse_ids": [], "skip": [], "attempt": 0, "stale_s": -1.0}
    phase = ck.get("phase") or ""
    if phase in TERMINAL_PHASES:
        return {"ok": False, "reason": "phase_terminal:%s" % phase, "mode": "", "checkpoint": ck,
                "requeue_ids": [], "reuse_ids": [], "skip": [],
                "attempt": int(ck.get("attempt_count") or 0), "stale_s": stale_seconds(conn, run_id)}
    attempt = int(ck.get("attempt_count") or 0)
    if attempt >= int(max_attempts or _MAX_ATTEMPTS):
        return {"ok": False, "reason": "attempt_exhausted:%d" % attempt, "mode": "", "checkpoint": ck,
                "requeue_ids": [], "reuse_ids": [], "skip": [],
                "attempt": attempt, "stale_s": stale_seconds(conn, run_id)}
    if is_alive(conn, run_id, stale_s):
        return {"ok": False, "reason": "still_alive", "mode": "", "checkpoint": ck,
                "requeue_ids": [], "reuse_ids": [], "skip": [],
                "attempt": attempt, "stale_s": stale_seconds(conn, run_id)}
    try:
        rows = [dict(r) for r in conn.execute(
            # ⚠️ `error_class` 必须一起取 —— 缺它的话 `policy_classify` 永远拿不到
            #   失败类别，于是"只重跑进程死亡类"这条判据**在结构上无法生效**
            #   （P0-1 之前正是这个形态：分类器造好了，但这条 SQL 不给它数据）。
            "SELECT id, task_key, status, deps, context, expected_output, agent_id, title, "
            "task_type, config, seq, error_class FROM agent_tasks WHERE run_id=? ORDER BY seq, id",
            (int(run_id),)).fetchall()]
    except Exception:
        rows = []
    if not rows:
        return {"ok": False, "reason": "no_tasks", "mode": "", "checkpoint": ck,
                "requeue_ids": [], "reuse_ids": [], "skip": [],
                "attempt": attempt, "stale_s": stale_seconds(conn, run_id)}
    _rqc = safe_requeue_classes() if requeue_classes is None else tuple(requeue_classes or ())
    rq, re_, sk = pick_requeue_ids(rows, run_has_owner=False, include_failed=include_failed,
                                   requeue_classes=_rqc)
    if not rq:
        # C-1：没有待重跑任务时，先问一句"是不是只是汇总崩了"——
        # 是则放行（mode=summary）；否则维持拒绝（真的没什么可做）。
        ok_sum, s_reason = summary_only_eligible(conn, run_id)
        if ok_sum:
            return {"ok": True, "reason": "ok", "mode": "summary", "checkpoint": ck,
                    "requeue_ids": [], "reuse_ids": re_, "skip": sk, "attempt": attempt,
                    "stale_s": stale_seconds(conn, run_id)}
        return {"ok": False, "reason": "nothing_to_do", "mode": "", "checkpoint": ck,
                "requeue_ids": [], "reuse_ids": re_, "skip": sk,
                "attempt": attempt, "stale_s": stale_seconds(conn, run_id),
                "detail": s_reason}
    return {"ok": True, "reason": "ok", "mode": "tasks", "checkpoint": ck, "requeue_ids": rq,
            "reuse_ids": re_, "skip": sk, "attempt": attempt,
            "stale_s": stale_seconds(conn, run_id)}


def mark_summary_written(conn, run_id: int) -> bool:
    """标记「汇总产物已与消息同事务落库」。

    ⚠️ 调用纪律：**必须与 assistant 消息 INSERT 处在同一个事务里**。
    若分两次提交，中途崩溃会留下「消息已落但标记仍为 0」→ resume 误判可重做汇总 → **重复消息**。
    这就是它必须写在 stream.py 那个 `with db_conn()` 块内的原因。
    """
    if not run_id:
        return False
    try:
        conn.execute("UPDATE orch_checkpoints SET summary_written=1, updated_at=CURRENT_TIMESTAMP "
                     "WHERE run_id=?", (int(run_id),))
        return True          # 不单独 commit：交由调用方所在事务统一提交
    except Exception:
        return False


def summary_only_eligible(conn, run_id: int) -> tuple:
    """能否「只重做汇总」（不重跑任何子任务）？返回 (bool, reason)。

    判据（三条全满足）：
      ① phase == summarizing                 —— 崩在汇总段，子任务已全部完成
      ② summary_written == 0                 —— 消息未落库，重做不会产生重复
      ③ **本编排开始之后**没有新增 assistant 消息 —— 冗余兜底：标记万一丢了也不重复
    第③条看着多余（②③本应同事务），但它正是"日后有人把 mark 挪出事务"时的最后一道闸：
    **宁可漏恢复，不可重复消息**（重复消息会污染会话历史，且用户几乎无法自行发现）。
    而它必须以 checkpoint 的 `created_at` 为界 —— 详见代码内注释（第一版按会话粒度查，
    曾让本功能彻底失效）。
    """
    ck = load(conn, run_id)
    if not ck:
        return False, "no_checkpoint"
    if (ck.get("phase") or "") != PHASE_SUMMARIZING:
        return False, "phase_not_summarizing:%s" % (ck.get("phase") or "")
    if int(ck.get("summary_written") or 0):
        return False, "summary_already_written"
    try:
        # ③ 冗余兜底：标记万一丢了也不重复。但判据必须是「**本编排开始之后**新增的」
        #    assistant 消息，**不能**是「该会话里有过任何 assistant 消息」。
        #    第一版写成会话粒度，端到端验证立刻抓到它**让本功能彻底失效**：
        #    任何走到编排阶段的会话都已有历史 assistant 消息（会话 514 实测 ≥6 条），
        #    于是 summary 通道永远判 false —— 兜底判据不与"这一次编排"绑定，
        #    就不再是兜底，而是一堵墙。
        started = str(ck.get("created_at") or "")
        if not started:
            return False, "no_ckpt_created_at"   # 无可比对的时间戳 ⇒ 宁可漏恢复
        row = conn.execute(
            "SELECT COUNT(*) FROM messages m JOIN agent_tasks t "
            "  ON t.conversation_id = m.conversation_id "
            "WHERE m.role='assistant' AND t.run_id=? AND t.status='done' "
            "AND m.created_at >= ?",
            (int(run_id), started)).fetchone()
        n = int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return False, "probe_failed"      # 查不了就当"已写过"（同"宁可漏恢复"原则")
    if n > 0:
        return False, "assistant_message_exists:%d" % n
    return True, "ok"


def apply_resume(conn, decision: dict) -> int:
    """执行 `prepare_resume` 的决策：把待重跑任务置回 `ready`（清错误），其余**一字不动**。

    刻意**不清** `result`/`metadata`：跳过的任务可能本就要保留其产出；
    且 ready 任务一旦被执行器成功 complete，result 会被整体覆盖。
    """
    ids = list((decision or {}).get("requeue_ids") or [])
    if not ids:
        return 0
    try:
        # ★ 2026-10-30：**同时清 error_class**。
        #   不清的话，被重排队的任务仍带着上一轮的 `interrupted` 标签 ——
        #   若它这一轮又因别的原因失败（且新的 error_class 没写入），
        #   下一轮会拿**旧的类别**再投一次，归因与决策一起失真。
        #   老库无该列时退回旧语句（不阻断 resume）。
        try:
            q = ("UPDATE agent_tasks SET status='ready', error='', error_class='', "
                 "updated_at=CURRENT_TIMESTAMP WHERE id IN (%s)" % ",".join("?" * len(ids)))
            conn.execute(q, tuple(int(i) for i in ids))
        except Exception:
            q = ("UPDATE agent_tasks SET status='ready', error='', updated_at=CURRENT_TIMESTAMP "
                 "WHERE id IN (%s)" % ",".join("?" * len(ids)))
            conn.execute(q, tuple(int(i) for i in ids))
        conn.commit()
        return len(ids)
    except Exception:
        return 0


def resumable(conn, conversation_id: int = 0, limit: int = 10,
              stale_s: int | None = None, requeue_classes=None) -> list:
    """列出"可恢复"的 run：非终态 phase + 心跳已超时 + 仍有待办任务 + 未超 resume 上限。

    ⚠️ "待办任务"含**两类**（P0-1，2026-10-30）：
      ① 非终态（planned/ready/running/blocked）—— 崩在半路；
      ② `status='failed'` 但 `error_class` 属可续跑类 —— 崩在宿主侧（进程重启）。
    只数①的话，②这类批次 `pending=0` ⇒ 被当成"没得恢复"直接跳过 ⇒
    `prepare_resume` 里那条新判据**永远没有机会被调用**（能力在、入口没接）。
    """
    try:
        q = ("SELECT run_id, conversation_id, phase, attempt_count, user_input, updated_at, "
             "(julianday('now') - julianday(updated_at)) * 86400.0 AS age_s "
             "FROM orch_checkpoints WHERE phase NOT IN ('done','failed')")
        args: list = []
        if conversation_id:
            q += " AND conversation_id=?"
            args.append(int(conversation_id))
        q += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit or 10))
        rows = [dict(r) for r in conn.execute(q, tuple(args)).fetchall()]
    except Exception:
        return []
    out = []
    for r in rows:
        rid = int(r.get("run_id") or 0)
        if not rid:
            continue
        try:
            age = float(r.get("age_s") or 0.0)
        except Exception:
            age = 0.0
        if age < float(stale_s if stale_s is not None else DEFAULT_STALE_S):
            continue        # 心跳还新 → 大概率正在正常运行，不给恢复入口
        if int(r.get("attempt_count") or 0) >= _MAX_ATTEMPTS:
            continue
        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM agent_tasks WHERE run_id=? AND status NOT IN ('done','failed','canceled')",
                (rid,)).fetchone()[0]
        except Exception:
            pending = 0
        # ★ ② 可续跑的 failed（进程死亡类）：必须计入 pending，否则这类批次被"没得恢复"挡死。
        _rqc = safe_requeue_classes() if requeue_classes is None else tuple(requeue_classes or ())
        if _rqc:
            try:
                _marks = ",".join("?" * len(_rqc))
                pending += int(conn.execute(
                    "SELECT COUNT(*) FROM agent_tasks WHERE run_id=? AND status='failed' "
                    "AND error_class IN (%s)" % _marks, (rid,) + tuple(_rqc)).fetchone()[0] or 0)
            except Exception:
                pass          # 老库无 error_class 列：当作没有（行为回到改动前）
        if not pending:
            # C-1：pending=0 不代表"没得恢复"—— 崩在汇总阶段时子任务全 done，
            # 但 assistant 消息尚未落库，这正是一个**可省掉整轮任务 LLM 费用**的恢复机会。
            ok_sum, _why = summary_only_eligible(conn, rid)
            if not ok_sum:
                continue
            pending = 0
        out.append({"run_id": rid, "conversation_id": int(r.get("conversation_id") or 0),
                    "phase": r.get("phase") or "", "attempt_count": int(r.get("attempt_count") or 0),
                    "pending": int(pending), "age_s": round(age, 1),
                    "summary_only": bool(summary_only_eligible(conn, rid)[0]),
                    "user_input": (r.get("user_input") or "")[:200]})
    return out
