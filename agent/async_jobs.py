# -*- coding: utf-8 -*-
"""异步外呼任务的**系统托管与跨轮续跑**（通用机制，不针对任何具体平台/工具）。

## 为什么需要这个模块（会话 9629 实测，连续 4 轮失败）

用户连续 4 次要求「获取工程 V2 代码」，每次都失败。真因**不是**取数逻辑写错，
而是**异步任务的责任没人负**：

| 轮次 | 实际发生 | 问题 |
|---|---|---|
| msg 4890 | 提交导出 → taskId `2114956963811217410` → **只查了 1 次** STATUS（progress=10）就收尾 | 模型谎称"连续查询了十余次"（`tool_call_logs` 只有 1 条 STATUS） |
| msg 4892 | 下一轮**从零重来**：重查工程/分支/版本、重新 PREVIEW、重新 EXPORT | taskId 未跨轮留存，前面白跑 |
| 8 小时后实测 | 两个 taskId **全部 `COMPLETED, progress=100`** | 任务本来会成功，只是"没人回来取" |

⇒ 单纯延长轮询窗口解决不了：Excel 式长任务动辄数分钟，**没有哪一轮对话该阻塞那么久**。
⇒ 正确做法是「**任务托管**」：提交那一刻系统接走 taskId，**下一轮开头自动查状态、
完成则自动分页读取并归档**，模型不需要记住任何东西；未完成的则明确告知"还在跑，
本轮别重新导出"。

## 协议判据（通用）

一次工具结果若**同时**满足：
① 可解析为 JSON 对象；② 含任务标识（`taskId` / `task_id` / `jobId`）；
③ 含状态字段（`status`）且取值落在已知集合内
⇒ 视为一个异步外呼任务，登记进 `async_external_jobs`。

读取/查询参数由结果**自描述**给出（`nextReadArguments` / `nextStatusArguments`），
没有则退回通用形态 `{"action": "READ"|"STATUS", "taskId": ...}` —— 不写死任何平台。
"""
import json
import time

TABLE = "async_external_jobs"

#: 视为「已提交但未完成」的状态
OPEN_STATES = {"SUBMITTED", "PENDING", "QUEUED", "RUNNING", "PROCESSING",
               "IN_PROGRESS", "WAITING", "STARTED"}
#: 视为「已成功完成」
DONE_STATES = {"COMPLETED", "SUCCESS", "SUCCEEDED", "FINISHED", "DONE"}
#: 视为「已失败/终止」
FAILED_STATES = {"FAILED", "ERROR", "CANCELLED", "CANCELED", "TIMEOUT", "ABORTED"}
#: 已完成但内容拼不出交付物的（不可读）—— 终态，不再重试，避免每轮空跑
UNREADABLE = "UNREADABLE"
#: 任务已不可查询（过期/无权限/被清理）—— 终态，允许重新提交
#: ★ 实测（conv 9446 的两个 10-17 任务）：服务端过期后返回
#:   `{"code":"PERMISSION_DENIED","message":"您没有数据访问权限",...}` —— **没有 status 字段**。
#:   若把"认不出状态"当成"还在跑"，这两个任务会被永久轮询、每轮都注入一条"仍在运行"，
#:   模型于是永远不敢重新提交 —— 这是"看起来在等、其实永远等不到"的典型死锁。
EXPIRED = "EXPIRED"
#: 轮询次数上限：超了就判过期（防"服务端一直说 RUNNING"的僵尸任务）
MAX_POLLS = 20

TASK_ID_KEYS = ("taskId", "task_id", "jobId", "job_id")


def ensure_table(conn) -> None:
    """建表（幂等）。不放进 schema.py 是为了让托管机制可独立开关/回滚。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS %s (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL DEFAULT 0,
            server TEXT DEFAULT '',
            tool_name TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL,
            status TEXT DEFAULT '',
            progress INTEGER DEFAULT 0,
            file_name TEXT DEFAULT '',
            meta TEXT DEFAULT '{}',
            artifact_id INTEGER DEFAULT 0,
            poll_count INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(conversation_id, task_id)
        )""" % TABLE)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_aej_conv ON %s(conversation_id, status)"
                     % TABLE)
    except Exception:
        pass


def _as_dict(result_text):
    t = (result_text or "").strip()
    if not t.startswith("{"):
        return None
    try:
        d = json.loads(t)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def detect_job(result_text) -> dict | None:
    """从工具结果里识别「异步外呼任务」。命中返回规范化 dict，否则 None。"""
    d = _as_dict(result_text)
    if not d:
        return None
    tid = ""
    for k in TASK_ID_KEYS:
        v = d.get(k)
        if v not in (None, ""):
            tid = str(v)
            break
    if not tid:
        return None
    raw_status = str(d.get("status") or d.get("state") or "").strip().upper()
    if raw_status not in (OPEN_STATES | DONE_STATES | FAILED_STATES):
        return None
    prog = 0
    try:
        prog = int(float(d.get("progress") or 0))
    except Exception:
        prog = 0
    meta = {}
    for k in ("nextReadArguments", "nextStatusArguments", "fileName", "mimeType",
              "sizeBytes", "fileAccess", "webDownloadUrl", "expiresAt", "message"):
        if k in d:
            meta[k] = d[k]
    return {"task_id": tid, "status": raw_status, "progress": prog,
            "file_name": str(d.get("fileName") or ""), "meta": meta}


def record(conn, conversation_id: int, server: str, tool_name: str, job: dict) -> None:
    """登记/更新一个异步任务（幂等）。已归档或已失败的不再覆盖成 open。

    ⚠️ 只在**提交/运行中**时登记：终态由 `update_status` 写入，避免"刚读到的旧
    RUNNING 结果把已归档记录又打回未完成"（重试/重复调用会产生先后矛盾的结果）。
    """
    ensure_table(conn)
    tid = job["task_id"]
    row = conn.execute("SELECT id, status, artifact_id FROM %s WHERE conversation_id=? AND task_id=?"
                       % TABLE, (conversation_id, tid)).fetchone()
    meta_json = json.dumps(job.get("meta") or {}, ensure_ascii=False)
    if row:
        _st = (row["status"] if not isinstance(row, tuple) else row[1]) or ""
        _art = (row["artifact_id"] if not isinstance(row, tuple) else row[2]) or 0
        if _st in DONE_STATES or _art:
            return
        conn.execute(
            "UPDATE %s SET status=?, progress=?, file_name=?, server=?, tool_name=?, "
            "meta=?, poll_count=poll_count+1, updated_at=CURRENT_TIMESTAMP WHERE id=?" % TABLE,
            (job["status"], job.get("progress") or 0, job.get("file_name") or "",
             server or "", tool_name or "", meta_json,
             row["id"] if not isinstance(row, tuple) else row[0]))
        return
    conn.execute(
        "INSERT INTO %s (conversation_id, server, tool_name, task_id, status, progress, "
        "file_name, meta) VALUES (?,?,?,?,?,?,?,?)" % TABLE,
        (conversation_id, server or "", tool_name or "", tid, job["status"],
         job.get("progress") or 0, job.get("file_name") or "", meta_json))


def open_jobs(conn, conversation_id: int, limit: int = 5) -> list:
    """待处理任务 = **尚未归档**且未失败/不可读。

    ⚠️ 判据是 `artifact_id=0`，**不是** `status != COMPLETED`：
    实测 9629 的任务 `2114940498336481282` 早已 COMPLETED，但那一轮归档因响应
    截断而失败（产物库 0 条）—— 若按"状态没完成才要处理"筛，这种
    「服务端已完成、本地没落地」的任务会被永久跳过，正是用户"白等"的另一半。
    """
    try:
        ensure_table(conn)
        terminal = sorted(FAILED_STATES) + [UNREADABLE, EXPIRED]
        rows = conn.execute(
            "SELECT * FROM %s WHERE conversation_id=? AND artifact_id=0 "
            "AND status NOT IN (%s) ORDER BY id DESC LIMIT ?" % (
                TABLE, ",".join("'%s'" % s for s in terminal)),
            (conversation_id, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def update_status(conn, task_id: str, status: str, *,
                  progress: int | None = None, artifact_id: int = 0,
                  last_error: str = "", poll: bool = True) -> None:
    try:
        ensure_table(conn)
        sets = ["status=?", "updated_at=CURRENT_TIMESTAMP"]
        args = [status]
        if progress is not None:
            sets.append("progress=?")
            args.append(progress)
        if artifact_id:
            sets.append("artifact_id=?")
            args.append(artifact_id)
        if last_error:
            sets.append("last_error=?")
            args.append(str(last_error)[:500])
        if poll:
            sets.append("poll_count=poll_count+1")
        args.append(task_id)
        conn.execute("UPDATE %s SET %s WHERE task_id=?" % (TABLE, ", ".join(sets)), args)
    except Exception:
        pass


# ── 读取参数：优先用结果自描述，其次退回通用形态 ──────────────────────────

def _status_args(job) -> dict:
    meta = job.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    nxt = meta.get("nextStatusArguments")
    if isinstance(nxt, dict) and nxt:
        return dict(nxt)
    return {"action": "STATUS", "taskId": job["task_id"]}


def _read_args(job, offset: int) -> dict:
    meta = job.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    nxt = meta.get("nextReadArguments")
    if isinstance(nxt, dict) and nxt:
        a = dict(nxt)
        a["offset"] = offset
        a["taskId"] = job["task_id"]
        return a
    return {"action": "READ", "taskId": job["task_id"],
            "offset": offset, "maxBytes": 32768}


def _mcp_server_of(conn, tool_name: str) -> str:
    try:
        rows = conn.execute(
            "SELECT name, tool_meta FROM mcp_servers WHERE status='online' OR enabled=1").fetchall()
        for r in rows:
            tm = (r["tool_meta"] if not isinstance(r, tuple) else r[1]) or ""
            if tool_name and ('"%s"' % tool_name) in tm:
                return r["name"] if not isinstance(r, tuple) else r[0]
    except Exception:
        pass
    return ""


def link_archived(conn, conversation_id: int, docs) -> int:
    """归档完成后把 `artifact_id` 回填到任务行，返回回填条数。

    ★ 为什么必须回填（本轮实测踩到）：续跑时只把任务标 `COMPLETED`，
      但产物是**稍后**（persist）才由 `_archive_artifacts` 创建的，当时还不知道 id。
      而 `open_jobs` 的判据是 `artifact_id=0`（正是为了捞"服务端已完成但本地没落地"的），
      ⇒ 若不回填，下一轮会**再读一遍 80KB**、并生成**重复产物**（每轮 message_id 不同，
      `_archive_artifacts` 的 (会话,消息,类型,标题) 幂等挡不住）。
      回填即收敛：`artifact_id != 0` ⇒ 不再进待处理。
    """
    n = 0
    try:
        for d in (docs or []):
            title = str((d or {}).get("title") or "").strip()
            if not title:
                continue
            row = conn.execute(
                "SELECT id FROM artifacts WHERE conversation_id=? AND title=? "
                "ORDER BY id DESC LIMIT 1", (conversation_id, title)).fetchone()
            if not row:
                continue
            aid = row["id"] if not isinstance(row, tuple) else row[0]
            cur = conn.execute(
                "UPDATE %s SET artifact_id=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE conversation_id=? AND artifact_id=0 AND (file_name=? OR file_name='')"
                % TABLE, (aid, conversation_id, title))
            n += cur.rowcount or 0
    except Exception:
        pass
    return n


def resume_jobs(pipe, conversation_id: int, max_reads: int = 40,
                max_jobs: int = 3, conn=None) -> dict:
    """**跨轮续跑**：查本会话所有未完成异步任务；完成的就地分页读全文。

    返回 ``{"docs": [...], "notes": [str], "running": [...], "failed": [...]}``

    - ``docs`` 已追加进 ``pipe._tool_sysml_docs``（persist 时由 `_archive_artifacts`
      统一登记成产物，**模型无需知情也无需配合**）；
    - ``notes`` 是给模型看的事实陈述（注入 system prompt）：已完成/仍在跑/已失败，
      用途是**阻止它重新导出**——实测模型每一轮都从零重来，把轮次全耗在重复劳动上。
    - ``conn`` 可选：传入则全程复用（**可测性**：门禁在真库临时副本上跑，
      否则本函数内部 `db_conn()` 永远读生产库，测试就成了假绿）。
    """
    out = {"docs": [], "notes": [], "running": [], "failed": []}
    if not conversation_id:
        return out
    try:
        from agent.utils import collect_tool_sysml_doc  # noqa: F401
    except Exception:
        return out

    def _with_conn(fn):
        """有外部 conn 就用它（不提交/不关闭，由调用方管）；否则开短连接。"""
        if conn is not None:
            return fn(conn)
        from database import db_conn
        with db_conn() as c2:
            return fn(c2)

    try:
        jobs = _with_conn(lambda c2: open_jobs(c2, conversation_id, limit=max_jobs))
    except Exception:
        return out
    if not jobs:
        return out

    for job in jobs:
        tname = job.get("tool_name") or ""
        tid = job.get("task_id") or ""
        if not tname or not tid:
            continue
        # ① 查状态
        try:
            st = pipe._exec_tool_call(tname, _status_args(job))
        except Exception as e:
            out["running"].append(tid)
            out["notes"].append(
                "【系统·异步任务】导出任务 %s 状态查询失败（%s），本轮请勿重新提交导出。"
                % (tid, str(e)[:80]))
            continue
        info = detect_job(str(st.get("result") or "")) or {}
        cur = (info.get("status") or "").upper()

        # ★ 认不出状态 ≠ 还在跑（实测过期任务返回 PERMISSION_DENIED，无 status 字段）。
        #   必须判成终态，否则"每轮都说仍在运行"会把任务锁死成永远等不到。
        if not cur:
            _raw = str(st.get("result") or "")
            _why = ""
            try:
                _d = json.loads(_raw) if _raw.strip().startswith("{") else {}
                _why = str(_d.get("code") or _d.get("error") or "")[:80]
            except Exception:
                _why = ""
            _why = _why or _raw[:80]
            if (not st.get("ok")) or _why:
                _with_conn(lambda c2: update_status(
                    c2, tid, EXPIRED, poll=False,
                    last_error="任务已不可查询：%s" % _why))
                out["failed"].append(tid)
                out["notes"].append(
                    "【系统·异步任务】上次的任务 %s 已无法查询（%s）——通常是过期或被清理，"
                    "不是还在运行。需要的话可以重新提交一次导出（不会与旧任务冲突）。"
                    % (tid, _why))
                continue

        # 僵尸任务：服务端反复回 RUNNING 但从不动；超过轮询上限即判过期
        try:
            _polls = int(job.get("poll_count") or 0)
        except Exception:
            _polls = 0
        if (cur not in DONE_STATES) and (cur not in FAILED_STATES) and _polls >= MAX_POLLS:
            _with_conn(lambda c2: update_status(
                c2, tid, EXPIRED, poll=False,
                last_error="轮询 %d 次仍未完成（进度 %s%%）" % (_polls, info.get("progress") or 0)))
            out["failed"].append(tid)
            out["notes"].append(
                "【系统·异步任务】任务 %s 已轮询 %d 次仍停在 %s%%（判定过期）。"
                "不要再等它，可以重新提交导出或改用按包范围导出。"
                % (tid, _polls, info.get("progress") or 0))
            continue

        if cur in FAILED_STATES:
            try:
                _with_conn(lambda c2: update_status(
                    c2, tid, cur, progress=info.get("progress"),
                    last_error=str(st.get("result") or "")[:300]))
            except Exception:
                pass
            out["failed"].append(tid)
            out["notes"].append(
                "【系统·异步任务】上次提交的导出任务 %s 已失败（状态 %s）。可以重新提交。"
                % (tid, cur))
            continue

        if cur not in DONE_STATES:
            try:
                _with_conn(lambda c2: update_status(
                    c2, tid, cur or "RUNNING", progress=info.get("progress") or 0))
            except Exception:
                pass
            out["running"].append(tid)
            out["notes"].append(
                "【系统·异步任务】上次提交的导出任务 %s 仍在服务端运行（状态 %s，进度 %s%%）。"
                "**本轮不要重新 PREVIEW/EXPORT**（重复提交只会再排一个新队）。"
                "请直接告知用户任务仍在生成、稍后再发一句「继续」，或转做其它能立刻完成的事。"
                % (tid, cur or "RUNNING", info.get("progress") or 0))
            continue

        # ② 已完成 → 分页读全文（不一次性要 80KB，避免单响应过大）
        pages = {}
        doc = None
        offset = 0
        for _ in range(max_reads):
            try:
                rr = pipe._exec_tool_call(tname, _read_args(job, offset))
            except Exception:
                break
            txt = str(rr.get("result") or "")
            doc = collect_tool_sysml_doc(pages, tname, txt)
            if doc:
                break
            d = _as_dict(txt) or {}
            nxt = d.get("nextOffset")
            if nxt is None:
                # 有些实现把"下一段参数"放在 nextReadArguments 里
                nra = d.get("nextReadArguments") or {}
                nxt = nra.get("offset") if isinstance(nra, dict) else None
            if nxt is None:
                break
            try:
                offset = int(nxt)
            except Exception:
                break
        if doc:
            pipe._tool_sysml_docs.append(doc)
            out["docs"].append(doc)
            out["notes"].append(
                "【系统·异步任务】上次提交的导出任务 %s **已完成**，系统已自动分页读取全文"
                "（%d 字符 / %d 字节）并登记为产物「%s」。**不要再重新导出**，"
                "直接告诉用户文件已在产物面板，可下载查看。"
                % (tid, len(doc["text"]), len(doc["text"].encode("utf-8")), doc["title"]))
            try:
                _with_conn(lambda c2: update_status(
                    c2, tid, "COMPLETED", progress=100, poll=False))
            except Exception:
                pass
        else:
            out["running"].append(tid)
            try:
                _with_conn(lambda c2: update_status(
                    c2, tid, UNREADABLE, progress=100, poll=False,
                    last_error="分页读取未能拼出完整文件（缺口/非交付物形态）"))
            except Exception:
                pass
            out["notes"].append(
                "【系统·异步任务】导出任务 %s 已完成，但**分页读取未能拼出完整文件**"
                "（可能有缺口或非交付物形态）。请如实告知用户读取失败，不要声称已归档。"
                % tid)
    return out
