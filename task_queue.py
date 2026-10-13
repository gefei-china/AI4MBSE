"""M3 多 Agent 任务队列：agent_tasks 表的通用访问层（Planner-Executor / Manager 共用）。

状态机：planned → ready（依赖就绪）→ running → done | failed | blocked | canceled
- create_plan：从 LLM 计划批量建任务
- ready_tasks / claim / complete / fail：执行器消费
- release_deps：任务完成后按依赖解锁下游任务（DAG 拓扑推进）
- handoff：结构化移交（summary + metadata），对齐 Hermes Kanban 语义的最小实现
"""
import json
import threading

_ALLOC_LOCK = threading.Lock()   # P0-7：批次号分配互斥（本仓单进程 uvicorn，进程内锁即足够）


class TaskQueue:
    """SQLite 持久化任务队列（零外部依赖，SQLite 连接由调用方传入）。"""

    STATUSES = ("planned", "ready", "running", "done", "failed", "blocked", "canceled")

    # ── 计划落库 ──
    @staticmethod
    def validate_plan(plan: list) -> dict:
        """★ 2026-10-13（B1-8）：落库前校验依赖图 —— 返回 {"ok", "reason", "cycle", "self_loop"}。

        为什么必须校验：原实现只把 `deps` 里**不存在的 key** 过滤掉，
        **不检出环**。一旦 LLM 产出 A→B→A（或 t1 依赖 t1），运行期后果是
        `ready_tasks` 永远返回空 → 主循环 `break` → **整批静默阻塞**，
        既不报错也无日志（用户只看到"什么都没发生"）。
        环是 planner 最常见的结构错误之一（多阶段任务里"生成→校验→回修"极易被写成环）。

        判据（Kahn 拓扑排序，O(V+E)）：
          · `self_loop`：任务依赖自己；
          · `cycle`：**存在环**（等价于拓扑排序后剩余节点非空）。
        ⚠️ 只做**检测与报错**，不静默改图（静默删边等于替 LLM 猜意图，
        且会让"为什么这条依赖没了"无从追溯 —— 那正是本条要修的病）。
        """
        try:
            rows = [t for t in (plan or []) if isinstance(t, dict)]
        except Exception:
            rows = []
        if not rows:
            return {"ok": False, "reason": "empty_plan", "cycle": [], "self_loop": []}

        keys = [str(t.get("key") or "") for t in rows]
        kset = set(keys)
        # 空 key：无法参与拓扑，单独报错（否则它会被当独立节点，依赖永远悬空）
        if any(not k for k in keys):
            return {"ok": False, "reason": "empty_key", "cycle": [], "self_loop": []}
        # 重复 key：后写的会覆盖先写的，且依赖语义不可解析
        if len(kset) != len(keys):
            dup = sorted({k for k in keys if keys.count(k) > 1})
            return {"ok": False, "reason": "duplicate_key", "cycle": [], "self_loop": dup}

        # 自环
        self_loop = sorted({str(t.get("key")) for t in rows
                            if str(t.get("key") or "") in {str(d) for d in (t.get("deps") or [])}})
        if self_loop:
            return {"ok": False, "reason": "self_loop", "cycle": [], "self_loop": self_loop}

        # 邻接表 + 入度（只计**本计划内存在**的依赖；外部 key 已被 create_plan 过滤）
        adj = {k: [] for k in keys}
        indeg = {k: 0 for k in keys}
        for t in rows:
            k = str(t.get("key") or "")
            for d in (t.get("deps") or []):
                ds = str(d)
                if ds in adj:          # 外部依赖不进图（与 create_plan 的过滤口径一致）
                    adj[ds].append(k)
                    indeg[k] += 1

        # Kahn：反复弹出入度 0 的节点；剩余即环内节点
        queue = [k for k in keys if indeg[k] == 0]
        seen = 0
        while queue:
            cur = queue.pop()
            seen += 1
            for nxt in adj[cur]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
        if seen < len(keys):
            remain = sorted([k for k in keys if indeg[k] > 0])
            return {"ok": False, "reason": "cycle", "cycle": remain, "self_loop": []}
        return {"ok": True, "reason": "", "cycle": [], "self_loop": []}

    @staticmethod
    def create_plan(conn, run_id: int, plan: list, assigned_by: str = "planner",
                    conversation_id: int = 0, *, strict: bool = True) -> int:
        """plan: [{key, title, agent, task_type, config, deps, seq, context, expected_output}] → 批量写入。

        依赖校验：deps 仅保留本计划内存在的 key（防脏数据导致永久阻塞）。
        P1b-2：context/expected_output 一并落库（委派协议结构化，子 Agent 执行时注入）。
        P0-7：conversation_id 记录批次归属会话（run_id 已唯一化，不再复用会话 id）。
        B1-8（2026-10-13）：**先跑 `validate_plan` 检环**（strict=True 时抛 ValueError）。
        ⚠️ 抛异常而不是打日志继续：环会让 `ready_tasks` 恒空 → 主循环 break → 整批静默阻塞，
        那种失败在界面上完全不可见。宁可让这一次编排明确失败，也不能"看起来在跑但什么都没做"。
        ⚠️ `strict=False` 保留旧行为（直接落库），供需要"尽力而为"的历史路径/门禁脚本显式选择。
        """
        if strict:
            _vd = TaskQueue.validate_plan(plan)
            if not _vd.get("ok"):
                raise ValueError(
                    "计划依赖图非法（reason=%s cycle=%s self_loop=%s），已中止落库以避免静默阻塞"
                    % (_vd.get("reason"), _vd.get("cycle"), _vd.get("self_loop")))
        plan_keys = {t.get("key") for t in plan}
        n = 0
        for t in plan:
            deps = [d for d in (t.get("deps") or []) if d in plan_keys]
            conn.execute(
                "INSERT INTO agent_tasks (run_id, task_key, title, agent_id, task_type, config, deps, "
                "status, assigned_by, seq, context, expected_output, conversation_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, t.get("key", ""), t.get("title", ""), t.get("agent", ""),
                 t.get("task_type", "agent"), json.dumps(t.get("config") or {}, ensure_ascii=False),
                 json.dumps(deps, ensure_ascii=False),
                 "ready" if not deps else "planned", assigned_by, t.get("seq", 0),
                 (t.get("context") or "")[:1000], (t.get("expected_output") or "")[:500],
                 int(conversation_id or 0)))
            n += 1
        conn.commit()
        if conversation_id:
            TaskQueue.prune_runs(conn, int(conversation_id))
        return n

    # ── P0-7：批次号分配与保留策略 ──
    @staticmethod
    def new_run_id(conn) -> int:
        """分配新的编排批次号（全局唯一、单调递增，进程内互斥）。

        P0-7：此前流式路径把 conversation_id 当 run_id、非流式路径恒传 0 —— 两者都让
        `clear_run` 在下一轮把上一轮计划整批删掉（实测会话 514：run=514 的 5 行被新一轮
        6 行覆盖）。改为独立批次号后，计划历史可回溯。
        """
        with _ALLOC_LOCK:
            row = conn.execute("SELECT COALESCE(MAX(run_id), 0) + 1 FROM agent_tasks").fetchone()
            return int((row[0] if row else 1) or 1)

    @staticmethod
    def prune_runs(conn, conversation_id: int, keep: int = 0) -> int:
        """保留某会话最近 N 次编排，清理更早的**终态**批次（防 agent_tasks 无界增长）。

        保守策略：含 running/ready/planned 的批次一律不动；失败静默（清理不是关键路径）。
        keep<=0 时读 config `delegation.keep_runs_per_conversation`（缺省 50）。
        """
        if not conversation_id:
            return 0
        if keep <= 0:
            try:
                from core import config as _cfg
                keep = int(_cfg.get("delegation", "keep_runs_per_conversation", 50) or 50)
            except Exception:
                keep = 50
        if keep <= 0:
            return 0
        try:
            # 用列下标取值：调用方连接可能未设 row_factory
            rows = conn.execute(
                "SELECT run_id, MAX(id) mx, "
                "SUM(CASE WHEN status IN ('running','ready','planned') THEN 1 ELSE 0 END) live "
                "FROM agent_tasks WHERE conversation_id=? GROUP BY run_id ORDER BY mx DESC",
                (int(conversation_id),)).fetchall()
            stale = [r[0] for r in rows[keep:] if not r[2]]
            if not stale:
                return 0
            q = ("DELETE FROM agent_tasks WHERE conversation_id=? AND run_id IN (%s)"
                 % ",".join("?" * len(stale)))
            conn.execute(q, tuple([int(conversation_id)] + stale))
            conn.commit()
            return len(stale)
        except Exception as _e:
            print("[task_queue] prune_runs 跳过：%s" % str(_e)[:120], flush=True)
            return 0

    @staticmethod
    def clear_run(conn, run_id: int) -> None:
        """清空某次运行的旧任务（重复运行前调用，防残留）。"""
        try:
            conn.execute("DELETE FROM agent_tasks WHERE run_id=?", (run_id,))
            conn.commit()
        except Exception:
            pass

    # ── 读取 ──
    @staticmethod
    def tasks(conn, run_id: int) -> list:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM agent_tasks WHERE run_id=? ORDER BY seq, id", (run_id,)).fetchall()]

    @staticmethod
    def ready_tasks(conn, run_id: int) -> list:
        """全部就绪（依赖已 done）任务，按 seq 排序。"""
        rows = conn.execute(
            "SELECT * FROM agent_tasks WHERE run_id=? AND status IN ('planned','ready') ORDER BY seq, id",
            (run_id,)).fetchall()
        done = {r["task_key"] for r in conn.execute(
            "SELECT task_key FROM agent_tasks WHERE run_id=? AND status='done'", (run_id,)).fetchall()}
        out = []
        for r in rows:
            deps = json.loads(r["deps"] or "[]")
            if not deps or all(d in done for d in deps):
                out.append(dict(r))
        return out

    @staticmethod
    def pending_count(conn, run_id: int) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_tasks WHERE run_id=? AND status NOT IN ('done','failed','canceled')",
            (run_id,)).fetchone()
        return row["n"] if row else 0

    # ── 状态推进 ──
    @staticmethod
    def claim(conn, task_id: int) -> None:
        """把任务标为执行中（planned/ready → running）。

        ★ 2026-10-13（B1-3）：首次被真实调用（此前零调用点，running 态库内 0 行）。
        **补 `commit()` 是必需的，不是风格问题**：conn 是编排主循环与 worker 线程**共用**的
        连接，python-sqlite3 在 UPDATE 时会隐式开事务；不提交则事务悬挂，
        主循环的心跳/pending_count 被拖住 → 实测 G1「活跃产出不误杀」判红
        （子任务持续吐 token 却被判 "timeout（1s 无产出）"）。
        本类其余状态推进（complete/fail/block/retry/resolve）**均带 commit**，claim 原本是唯一例外。
        """
        conn.execute(
            "UPDATE agent_tasks SET status='running', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('planned','ready')",
            (task_id,))
        conn.commit()

    @staticmethod
    def _result_keep_chars() -> int:
        """子任务结果落库保留上限（字符）。2026-09-20 由硬编码 4000 改为可配置。

        原硬编码 4000 会把超过 4,000 字符的交付物**静默截尾**（会话 368 实测 t2/t3 恰好卡在
        4000 = 被截断），而汇总环节还会再切一刀 → 两层截断叠加，交付物在报告里"整体缺结论"。
        保留上限本身仍有必要（防单行结果撑爆库），故只做「配置化 + 抬高默认」，不去掉。
        """
        from core import config as _cfg
        try:
            v = int(_cfg.get("delegation", "subtask_result_keep_chars", 20000) or 20000)
        except Exception:
            v = 20000
        return v if v > 0 else 0        # <=0 = 不限长

    @staticmethod
    def complete(conn, task_id: int, result: str = "", metadata: dict | None = None,
                 latency_ms: int = 0) -> None:
        """完成任务 + 结构化 handoff metadata（summary/artifacts/score）。"""
        _keep = TaskQueue._result_keep_chars()
        _res = result if _keep <= 0 else result[:_keep]
        conn.execute(
            "UPDATE agent_tasks SET status='done', result=?, metadata=?, latency_ms=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (_res, json.dumps(metadata or {}, ensure_ascii=False), latency_ms, task_id))
        conn.commit()

    @staticmethod
    def fail(conn, task_id: int, error: str = "") -> None:
        """标记失败，并**结构化记录失败分类**（★ 2026-10-13 B1-6）。

        为什么顺带分类：`error` 是自由文本，事后无法统计"哪类失败最多 / 哪些白重试了"。
        `error_class` 落库后，"这个任务为什么失败、该不该重试"才在数据层可回答。
        ⚠️ 分类器不可用时**不阻断失败流程**（error_class 留空），且不会把 error 吞掉。
        """
        _cls = ""
        try:
            from services import failure_class as _fc
            _cls = _fc.classify(error, ok=False)
        except Exception:
            _cls = ""
        try:
            conn.execute(
                "UPDATE agent_tasks SET status='failed', error=?, error_class=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (error[:500], _cls, task_id))
            conn.commit()
        except Exception:
            # 极老库可能尚无 error_class 列 ⇒ 退回不分类的写入，保证失败仍被记录
            try:
                conn.execute(
                    "UPDATE agent_tasks SET status='failed', error=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?", (error[:500], task_id))
                conn.commit()
            except Exception:
                pass

    @staticmethod
    def block(conn, task_id: int, error: str = "") -> None:
        """失败但依赖下游继续（弱失败）→ blocked（人工可介入恢复）。"""
        conn.execute(
            "UPDATE agent_tasks SET status='blocked', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (error[:500], task_id))
        conn.commit()

    # ── DAG 推进 ──
    @staticmethod
    def retry(conn, task_id: int) -> None:
        """P2 人工重试：blocked/failed → ready（重新进入执行队列，清错误）。"""
        conn.execute(
            "UPDATE agent_tasks SET status='ready', error='', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (task_id,))
        conn.commit()

    @staticmethod
    def resolve(conn, task_id: int, note: str = "") -> None:
        """P2 人工确认完成：blocked → done（结果标注人工介入），并解锁下游依赖。"""
        row = conn.execute("SELECT task_key, run_id FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        conn.execute(
            "UPDATE agent_tasks SET status='done', metadata=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps({"resolved_by": "human", "note": note or "人工确认完成"}, ensure_ascii=False), task_id))
        conn.commit()
        if row:
            TaskQueue.release_deps(conn, row["run_id"], row["task_key"])

    @staticmethod
    def release_deps(conn, run_id: int, task_key: str) -> list:
        """task_key 完成后，解锁依赖它的任务（deps 全部 done → ready）。返回被解锁的任务 key 列表。"""
        done = {r["task_key"] for r in conn.execute(
            "SELECT task_key FROM agent_tasks WHERE run_id=? AND status='done'", (run_id,)).fetchall()}
        unlocked = []
        rows = conn.execute(
            "SELECT id, task_key, deps FROM agent_tasks WHERE run_id=? AND status='planned'", (run_id,)).fetchall()
        for r in rows:
            deps = json.loads(r["deps"] or "[]")
            if deps and all(d in done for d in deps):
                conn.execute("UPDATE agent_tasks SET status='ready', updated_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
                unlocked.append(r["task_key"])
        conn.commit()
        return unlocked

    # ── 汇总 ──
    @staticmethod
    def summary(conn, run_id: int) -> dict:
        """运行级汇总：按状态计数 + 各任务 handoff 视图（供 Manager/Planner 汇总消费）。"""
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM agent_tasks WHERE run_id=? GROUP BY status", (run_id,)).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        items = [dict(r) for r in conn.execute(
            "SELECT task_key, title, agent_id, status, result, metadata, error, latency_ms "
            "FROM agent_tasks WHERE run_id=? ORDER BY seq, id", (run_id,)).fetchall()]
        return {"counts": counts, "total": len(items), "items": items}
