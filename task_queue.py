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
    def create_plan(conn, run_id: int, plan: list, assigned_by: str = "planner",
                    conversation_id: int = 0) -> int:
        """plan: [{key, title, agent, task_type, config, deps, seq, context, expected_output}] → 批量写入。

        依赖校验：deps 仅保留本计划内存在的 key（防脏数据导致永久阻塞）。
        P1b-2：context/expected_output 一并落库（委派协议结构化，子 Agent 执行时注入）。
        P0-7：conversation_id 记录批次归属会话（run_id 已唯一化，不再复用会话 id）。
        """
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
        conn.execute(
            "UPDATE agent_tasks SET status='running', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('planned','ready')",
            (task_id,))

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
        conn.execute(
            "UPDATE agent_tasks SET status='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (error[:500], task_id))
        conn.commit()

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
