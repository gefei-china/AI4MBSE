"""Agent 架构优化「交付物物化」模块（Task 3）。

背景：多 Agent 编排（agent/pipeline.py _stream_orchestrated_flow）子任务以 dry_run=True
执行（不写正式 messages/audit），产物散落在事件流中。本模块把子任务产物物化到独立
命名空间表 subtask_artifacts（Task 1 已建表），提供：

- store / resolve：产物写入与 subtask:// ref 解析（ref 格式 subtask://{run_id}/{task_key}/a{idx}）
- store_write_request / list_write_requests / batch_queue_confirmations：
  写工具请求暂存 → 汇总后批量挂 HIL 人工确认队列
- materialize_to_conversation：独立命名空间产物 → 正式会话产物（二次物化，走 artifacts 登记）
- enforce_retention：保留策略（按 run_id 去重只保留最近 keep_runs 个 run）

供 Task 7/8/9 使用；本次仅建模块 + 自测。
"""
import json

REF_PREFIX = "subtask://"

# kind → 二次物化登记 artifacts 表的 kind（对齐 artifacts 表 kind 取值：report|code|sysml|document|other）
_MATERIALIZE_KIND_MAP = {
    "sysml": "sysml",
    "requirement": "report",
    "report": "report",
    "code": "code",
    "document": "document",
    "doc": "document",
    "other": "other",
}


def _parse_ref(ref: str) -> tuple | None:
    """subtask://{run_id}/{task_key}/a{idx} → (run_id, task_key, idx)；非法 ref 返回 None。"""
    if not ref or not ref.startswith(REF_PREFIX):
        return None
    parts = ref[len(REF_PREFIX):].split("/")
    if len(parts) != 3 or not parts[2].startswith("a"):
        return None
    run_id_s, task_key, idx_s = parts
    try:
        run_id = int(run_id_s)
        idx = int(idx_s[1:])
    except (TypeError, ValueError):
        return None
    if not run_id_s or not task_key:
        return None
    return run_id, task_key, idx


def _serialize_content(content) -> str:
    """content_json 归一化：dict/list → JSON 串；str 原样；其余兜底序列化。"""
    if isinstance(content, str):
        return content
    return json.dumps(content if content is not None else {}, ensure_ascii=False)


def store(conn, run_id, task_key, artifacts: list[dict]) -> list[str]:
    """把产物列表（{kind,title,content_json}）写入 subtask_artifacts（artifact_idx 递增）。

    返回生成的 ref 列表（与输入产物一一对应）；空列表返回 []。
    content_json 接受 dict/str：dict 直接序列化，str 原样落库。
    """
    if not artifacts:
        return []
    refs = []
    for art in artifacts or []:
        if not isinstance(art, dict):
            continue
        row = conn.execute(
            "SELECT MAX(artifact_idx) m FROM subtask_artifacts WHERE run_id=? AND task_key=?",
            (run_id, task_key)).fetchone()
        idx = (row["m"] if row and row["m"] is not None else 0) + 1
        conn.execute(
            "INSERT INTO subtask_artifacts (run_id, task_key, artifact_idx, kind, title, content_json) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, task_key, idx,
             (art.get("kind") or "other"), (art.get("title") or ""),
             _serialize_content(art.get("content_json") or {})))
        refs.append(f"{REF_PREFIX}{run_id}/{task_key}/a{idx}")
    conn.commit()
    return refs


def resolve(conn, ref) -> dict | None:
    """解析 subtask:// ref 返回物化行（含 content_json 反序列化）；非法/未命中返回 None。"""
    parsed = _parse_ref(ref)
    if not parsed:
        return None
    run_id, task_key, idx = parsed
    row = conn.execute(
        "SELECT * FROM subtask_artifacts WHERE run_id=? AND task_key=? AND artifact_idx=?",
        (run_id, task_key, idx)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["content_json"] = json.loads(d.get("content_json") or "{}")
    except Exception:
        d["content_json"] = {}
    return d


def store_write_request(conn, run_id, task_key, tool_name, arguments) -> str:
    """写工具请求暂存：kind='write_request'，title=工具名，
    content_json={"tool": tool_name, "arguments": {...}}，返回 ref。"""
    content = {"tool": tool_name, "arguments": arguments or {}}
    refs = store(conn, run_id, task_key, [{
        "kind": "write_request", "title": tool_name, "content_json": content,
    }])
    return refs[0] if refs else ""


def list_write_requests(conn, run_id) -> list[dict]:
    """某 run 的全部 write_request（按 id 升序，含反序列化 content_json）。"""
    rows = conn.execute(
        "SELECT * FROM subtask_artifacts WHERE run_id=? AND kind='write_request' ORDER BY id ASC",
        (run_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["content_json"] = json.loads(d.get("content_json") or "{}")
        except Exception:
            d["content_json"] = {}
        out.append(d)
    return out


def batch_queue_confirmations(conn, run_id, agent_id="", conversation_id=0) -> int:
    """汇总后批量挂确认队列：遍历该 run 全部 write_request，
    调用 hil_service.HILService.queue_confirmation(...)，返回入队数。

    入队成功后把 write_request 标记已处理（materialized=1），防重复入队。
    """
    from hil_service import HILService
    queued = 0
    for r in list_write_requests(conn, run_id):
        if r.get("materialized"):
            continue
        cj = r.get("content_json") or {}
        tool_name = cj.get("tool") or r.get("title") or ""
        arguments = cj.get("arguments") or {}
        preview = ("子任务写操作暂存：{tool}，参数：{args}"
                   .format(tool=tool_name, args=json.dumps(arguments, ensure_ascii=False)[:300]))
        conf = HILService.queue_confirmation(
            conn, agent_id or "", tool_name, arguments,
            # P0-7：run_id 唯一化后不再等于会话 id → conversation_id 必须显式传入
            # （缺省回退 run_id，兼容未改的旧调用方）
            preview=preview, conversation_id=int(conversation_id or run_id or 0),
            run_id=int(run_id or 0),
            task_key=r.get("task_key") or "")
        if conf:
            queued += 1
            conn.execute("UPDATE subtask_artifacts SET materialized=1 WHERE id=?", (r["id"],))
    conn.commit()
    return queued


def materialize_to_conversation(conn, run_id, task_key, ref, conversation_id, message_id) -> int:
    """二次物化：把独立命名空间产物写为正式会话产物（artifacts 登记）。

    接入方式：按项目既有 artifacts 登记逻辑插入一条 artifacts 记录
    （preview_content=content_json 序列化，meta 含 subtask_ref/run_id/task_key 溯源），
    并把 subtask_artifacts.materialized 置 1。返回受影响物化行数（0=ref 未命中/不可物化）。
    write_request 暂存不物化为会话产物（由 batch_queue_confirmations 挂确认队列）。
    """
    parsed = _parse_ref(ref)
    if not parsed:
        return 0
    r_run_id, r_task_key, idx = parsed
    row = conn.execute(
        "SELECT * FROM subtask_artifacts WHERE run_id=? AND task_key=? AND artifact_idx=?",
        (r_run_id, r_task_key, idx)).fetchone()
    if not row:
        return 0
    d = dict(row)
    kind = d.get("kind") or "other"
    if kind == "write_request":
        return 0
    try:
        content = json.loads(d.get("content_json") or "{}")
    except Exception:
        content = {}
    title = d.get("title") or f"{kind}产物"
    if isinstance(content, dict):
        preview_content = json.dumps(content, ensure_ascii=False)
        meta = dict(content)
    else:
        preview_content = content if isinstance(content, str) else str(content)
        meta = {}
    meta["subtask_ref"] = ref
    meta["subtask_run_id"] = d.get("run_id")
    meta["subtask_task_key"] = d.get("task_key")
    # P1-1（2026-09-28）：产物归属在**写入时定格**（会话归属，空=无工程会话，合法）
    from repositories.project_repo import conversation_project_id
    conn.execute(
        "INSERT INTO artifacts (conversation_id, message_id, project_id, kind, title, preview_type, preview_content, meta, source, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (conversation_id or 0, message_id or 0,
         conversation_project_id(conn, conversation_id or 0),
         _MATERIALIZE_KIND_MAP.get(kind, "other"), title,
         "markdown" if kind in ("report", "requirement", "doc", "document") else "text",
         preview_content,
         json.dumps(meta, ensure_ascii=False),
         "flow", ""))
    conn.execute("UPDATE subtask_artifacts SET materialized=1 WHERE id=?", (d["id"],))
    conn.commit()
    return 1


def enforce_retention(conn, keep_runs=20) -> int:
    """保留策略：subtask_artifacts 只保留最近 keep_runs 个不同 run_id
    （按 run_id 去重，以各 run 最大物化行 id 取最新 N 个 run；删除更早 run 的全部物化行）。

    返回删除行数。简单实现：对全部早于窗口的 run 清理（含已物化/已汇总行）。
    """
    keep_runs = max(1, int(keep_runs if keep_runs is not None else 20))
    rows = conn.execute(
        "SELECT run_id, MAX(id) AS max_id FROM subtask_artifacts "
        "GROUP BY run_id ORDER BY max_id DESC").fetchall()
    keep_run_ids = [r["run_id"] for r in rows[:keep_runs]]
    if not keep_run_ids:
        return 0
    placeholders = ",".join("?" for _ in keep_run_ids)
    del_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM subtask_artifacts WHERE run_id NOT IN ({placeholders})",
        keep_run_ids).fetchone()
    deleted = del_row["n"] if del_row else 0
    if deleted:
        conn.execute(
            f"DELETE FROM subtask_artifacts WHERE run_id NOT IN ({placeholders})",
            keep_run_ids)
        conn.commit()
    return deleted
