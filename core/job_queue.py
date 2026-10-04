# -*- coding: utf-8 -*-
"""P0-C（2026-10-04）：进程内异步作业队列 —— 提交即返回 job_id。

## 问题（实测）
长任务此前只能**同步占着 HTTP 连接**跑完：
- 工程入库实测 **13.4 分钟**（三份规范）；
- 单轮编排实测 **344~1155 s**。
后果：① 请求断开即前功尽弃；② 期间该连接占住一个 anyio 线程池名额（上限 40）。
`batch_id` 只是"数据批次标识"，**不是异步作业句柄** —— 已逐处核实（`project_ingest_commit`
等仍是同步执行）。所以"提交即返回 + 进度可查"这个能力此前确实缺失。

## 为什么不用 Redis + RQ（这是被约束逼出来的选择，不是偷懒）
`AGENTS.md` **铁律 4：私有化离线部署，不引入外部 CDN / 构建链 / 新依赖**。
Redis 方案直接违反该铁律；本机实测也无 redis-server / redis-cli / 6379 无响应。
⇒ 用 SQLite 承载队列状态（本仓已有 WAL + busy_timeout，单写者对本用途足够），
消费端是后台 daemon worker —— 与 `core/orch_supervisor.py` 同一范本，复用其踩过的坑。

## 核心语义
- **幂等提交**：`job_key` 唯一 ⇒ 同 key 重复提交**返回既有 job_id**而不是重复执行。
  这是"用户手抖点两下"的唯一防线（对标 Temporal 的 idempotency key）。
- **租约（lease）而非硬状态**：worker claim 时写 `lease_until` + `heartbeat_at`；
  进程被kill 时租约会自然过期 ⇒ 下一个 worker 能回收。**不能只靠 status='running'
  判断"是否有人在做"** —— 崩了的作业会永远停在 running（这正是批次 1 孤儿问题的同型）。
- **重试有上限**：`attempt >= max_attempts` 转 failed，不无限自愈。
- **进度不精确**：progress/stage 是给人看的；**判据只取 status**（终态/非终态），
  不拿进度做业务判断（这条与 orch_checkpoint 踩过的"兜底判据锚错"同源）。

## 取舍声明
进程内队列**不跨进程**：单副本下完全够用（本仓 `docker-compose.yml` 明写单副本是硬约束，
理由就是 SQLite 单写者）。若将来上多副本，此模块必须换成真正的外部队列
——那时 `job_jobs` 表可平滑迁移为"任务登记表"，但 worker 领取机制要重做。
"""
import json
import threading
import time
import traceback
import uuid

__all__ = ["submit", "get", "cancel", "pick_next", "heartbeat", "report",
           "set_conn_factory",
           "progress", "register_handler", "run_worker_loop", "start_workers",
           "stop_workers", "queue_stats"]

# ── 终态与非终态 ──
TERMINAL = ("done", "failed", "canceled")
ACTIVE = ("queued", "running")

_AUTO_SEQ = 0           # 自动 job_key 的进程内自增序号（配合 uuid 防撞）

_HANDLERS = {}          # kind -> callable(payload: dict, job: dict) -> dict
_worker_state = {"threads": [], "stop": threading.Event(), "worker_id": ""}
_conn_factory = None    # 可注入的连接工厂（测试/多库场景；None ⇒ 用 database.get_db）


def set_conn_factory(fn) -> None:
    """注入连接工厂（返回**已设好 row_factory 的 sqlite3.Connection**）。

    为什么需要：`database.get_db()` 里的 `DB_PATH` 是**import 期绑定**的模块常量
    （`connection.py:8from core.config import DB_PATH`），所以
    「改了 `core.config.DB_PATH` 但没改 `database.connection.DB_PATH`」时
    worker 仍会连到生产库 —— 我实测踩过（worker 领到任务后状态永远停在 running，
    因为它在一个没建 job_jobs 表的库上 UPDATE 失败被 except 吞掉）。
    注入工厂让调用方显式决定连哪个库，不必依赖 monkeypatch 时机。
    """
    global _conn_factory
    _conn_factory = fn


def _conn():
    """取一个连接（优先注入的工厂）。"""
    if _conn_factory is not None:
        return _conn_factory()
    from database import get_db
    return get_db()


def _cfg(section: str, key: str, default=None):
    try:
        from core import config as _c
        return _c.get(section, key, default)
    except Exception:
        return default


# ══════════════════════════════════════════════════════════════════
# 提交 / 查询
# ══════════════════════════════════════════════════════════════════
def submit(conn, kind: str, payload: dict | None = None, *, job_key: str = "",
           max_attempts: int = 3) -> dict:
    """提交一个作业并**立即返回**（不等待执行）。返回 `{job_id, status, deduped}`。

    :param job_key: 业务幂等键。**强烈建议传**（如"project_ingest:<pid>:<ts>"）：
        同一 key 重复提交只会返回既有 job，不会重复执行 ——
        这是"用户手抖点两下"的唯一防线，也是 Temporal idempotency key 的等价物。
    :returns: deduped=True 表示命中已有作业（未重复执行）
    """
    k = (job_key or "").strip()
    if not k:
        # ⚠️ 必须保证**每次都不撞车**：第一版用 `time.time()*1000 + time.time()%1`，
        #   实测两次快速提交得到同一个 key（3/3）⇒ 第二个作业被幂等逻辑误判为重复
        #   ⇒ **静默丢作业**（比报错更糟）。
        #   改用 `uuid4` + 进程内自增序号，彻底消除撞车可能。
        global _AUTO_SEQ
        _AUTO_SEQ += 1
        k = "%s:%s:%d:%d" % (kind or "job", uuid.uuid4().hex[:8],
                            int(time.time() * 1000), _AUTO_SEQ)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    # 幂等：先查既有
    if k:
        r = conn.execute(
            "SELECT id, status, progress, stage FROM job_jobs WHERE job_key=? LIMIT 1",
            (k,)).fetchone()
        if r:
            d = _row(r)
            if "id" in d:
                return {"job_id": int(d["id"]), "status": d.get("status"),
                        "deduped": True, "progress": int(d.get("progress") or 0),
                        "stage": d.get("stage") or ""}
    cur = conn.execute(
        "INSERT INTO job_jobs (job_key, kind, payload, status, max_attempts, "
        "created_at, updated_at) VALUES (?,?,?,'queued',?,?,?)",
        (k, kind or "", json.dumps(payload or {}, ensure_ascii=False, default=str),
         int(max_attempts or 3), now, now))
    conn.commit()
    return {"job_id": int(cur.lastrowid), "status": "queued", "deduped": False,
            "progress": 0, "stage": ""}


def _row(row) -> dict:
    """把一行转成 dict。

    ⚠️ **不能直接用 `row["col"]`**：本仓的 `get_db()` 设了 `sqlite3.Row`，
    但**裸 `sqlite3.connect()` 建立的连接没有**（我实测踩过：`row["id"]` 直接
    TypeError: tuple indices must be integers）。让本模块对两种 row 都成立，
    省掉调用方的环境假设。
    """
    if row is None:
        return {}
    try:
        return dict(row)          # sqlite3.Row → dict
    except Exception:
        pass
    try:
        return {"id": row[0]}    # tuple：至少保id 可用，其余由调用方自行处理
    except Exception:
        return {}


def get(conn, job_id: int) -> dict | None:
    """查作业状态（含结果/错误/阶段）。不存在返回 None。"""
    r = conn.execute("SELECT * FROM job_jobs WHERE id=?", (int(job_id),)).fetchone()
    if not r:
        return None
    d = _row(r)
    if "id" not in d:
        # tuple 型row：按列顺序补全（job_jobs 的建表列序固定，见 schema.py）
        cols = ["id", "job_key", "kind", "payload", "status", "progress", "stage",
                "result", "error", "attempt", "max_attempts", "lease_until",
                "heartbeat_at", "worker_id", "created_at", "updated_at"]
        vals = list(r)
        d = {c: (vals[i] if i < len(vals) else None) for i, c in enumerate(cols)}
    return d


def cancel(conn, job_id: int) -> dict:
    """取消作业。**只对未开始或已失活的生效**：
    `running` 且租约未过期 ⇒ 拒绝（可能真在跑，强杀会留下半个副作用）。
    这条与"断连止损"是同一类判断：宁可漏取消，不可重复副作用。
    """
    job = get(conn, job_id)
    if not job:
        return {"ok": False, "error": "作业不存在"}
    if job["status"] in TERMINAL:
        return {"ok": False, "error": "作业已终态（%s），无法取消" % job["status"],
                "status": job["status"]}
    if job["status"] == "running" and not lease_expired(job):
        return {"ok": False, "error": "作业正在执行中，取消需等租约过期或停止 worker",
                "status": job["status"]}
    conn.execute("UPDATE job_jobs SET status='canceled', updated_at=? "
                 "WHERE id=? AND status NOT IN ('done','failed','canceled')",
                 (time.strftime("%Y-%m-%d %H:%M:%S"), int(job_id)))
    conn.commit()
    return {"ok": True, "status": "canceled"}


def lease_expired(job: dict) -> bool:
    """租约是否已过期（进程崩溃后的回收判据）。

    ⚠️ 时间比较**必须在 Python 侧做且用本地时间**：`lease_until` 由本模块用
    `time.strftime` 写入（本地时间），而 SQLite 的 `CURRENT_TIMESTAMP` 是 **UTC**。
    混算会偏移 8 小时 —— 这正是 `orch_checkpoint` 踩过的坑（那里改成 SQL 内
    `julianday` 比较，本模块因写入源不同而必须走Python侧）。
    """
    lu = (job or {}).get("lease_until") or ""
    if not lu:
        return True
    try:
        return time.time() > time.mktime(time.strptime(lu, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return True     # 解析不了 ⇒ 视为过期（宁可回收一次，也不要永久卡住）


def queue_stats(conn) -> dict:
    """队列概览（按状态计数 + 最老待处理年龄），供监控端点。"""
    out = {}
    for r in conn.execute("SELECT status, COUNT(*) c FROM job_jobs GROUP BY 1"):
        d = _row(r)
        out[d.get("status")] = int(d.get("c") or 0)
    try:
        r = conn.execute(
            "SELECT MIN(created_at) a FROM job_jobs WHERE status IN ('queued','running')"
        ).fetchone()
        d = _row(r)
        out["oldest_active"] = (d.get("a") or "") if d else ""
    except Exception:
        out["oldest_active"] = ""
    out["total"] = sum(v for k, v in out.items() if k in TERMINAL + ACTIVE)
    return out


# ══════════════════════════════════════════════════════════════════
# worker 侧：领取 / 心跳 / 回报
# ══════════════════════════════════════════════════════════════════
def register_handler(kind: str, fn) -> None:
    """注册作业处理器：`fn(payload: dict, job: dict) -> dict`。
    返回值会JSON 序列化落进 `result`。抛异常 ⇒ 记 failed（按 attempt 决定是否重试）。
    """
    _HANDLERS[kind] = fn


def pick_next(conn, worker_id: str = "", lease_s: int = 300) -> dict | None:
    """领取下一个可执行的作业。**必须带租约**，否则崩溃后作业会永远停在 running。

    可领取 = `queued`（从未开始）**或** `running` 且**租约已过期**（上个 worker 崩了）。
    第二条是P0-2b 孤儿回收的同型问题：`status='running'` 本身**不能**证明有人在做。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lease = time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(time.time() + int(lease_s or 300)))
    rows = conn.execute(
        "SELECT * FROM job_jobs WHERE status='queued' "
        "ORDER BY id LIMIT 20").fetchall()
    pick = None
    for r in rows:
        d = _row(r)
        lu = (d.get("lease_until") or "").strip()[:19]
        # ⚠️ 这里必须**显式判定空租约 = 可领取**。
        #   第一版写成"解析失败 ⇒ deadline=time.time()" 再做 `time.time() > deadline`，
        #   结果取决于两次 time.time() 之间有没有耗时间 ⇒ **竞态**，
        #   表现为"同一个作业有时能领到、有时领不到"，门禁 Q3d 曾因此随机红。
        # 判据：空 = 没人持有（queued 作业本来就没有持有者）。
        if not lu:
            pick = d
            break
        try:
            deadline = time.mktime(time.strptime(lu, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            pick = d            # 垃圾租约 ⇒ 视为可领取（宁可多跑一次，不要永久卡住）
            break
        if time.time() > deadline:
            pick = d
            break
    # 顺带回收失活running（attempt 不加，回收不是"重试"）
    for r in conn.execute(
            "SELECT id FROM job_jobs WHERE status='running' AND "
            "(lease_until='' OR lease_until < ?) LIMIT 1", (now,)).fetchall():
        d = _row(r)
        if d.get("id") and not pick:
            pick = get(conn, int(d["id"]))
            break
    if not pick:
        return None
    try:
        conn.execute(
            "UPDATE job_jobs SET status='running', worker_id=?, lease_until=?, "
            "heartbeat_at=?, attempt=attempt+1, updated_at=? WHERE id=? AND "
            "status IN ('queued','running')",
            (worker_id or "w1", lease, now, now, int(pick["id"])))
        conn.commit()
    except Exception:
        return None
    return get(conn, int(pick["id"]))


def heartbeat(conn, job_id: int, lease_s: int = 300) -> bool:
    """续租（长作业执行中必须定期调用，否则会被别的 worker 回收 ⇒ 双跑）。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lease = time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(time.time() + int(lease_s or 300)))
    try:
        conn.execute("UPDATE job_jobs SET lease_until=?, heartbeat_at=?, updated_at=? "
                     "WHERE id=?", (lease, now, now, int(job_id)))
        conn.commit()
        return True
    except Exception:
        return False


def progress(conn, job_id: int, percent: int | None = None, stage: str = "") -> None:
    """上报进度/阶段（**给人看**）。业务判据只取 status，不依赖 progress。"""
    sets, args = [], []
    if percent is not None:
        sets.append("progress=?")
        args.append(max(0, min(100, int(percent))))
    if stage:
        sets.append("stage=?")
        args.append(str(stage)[:200])
    if not sets:
        return
    sets.append("updated_at=?")
    args.append(time.strftime("%Y-%m-%d %H:%M:%S"))
    args.append(int(job_id))
    try:
        conn.execute("UPDATE job_jobs SET %s WHERE id=?" % ",".join(sets), tuple(args))
        conn.commit()
    except Exception:
        pass


def report(conn, job_id: int, ok: bool, result: dict | None = None,
           error: str = "") -> dict:
    """回报终态。失败且未超 `max_attempts` ⇒ 回退 `queued` 等待重试（**不直接 failed**）。"""
    job = get(conn, job_id)
    if not job:
        return {"ok": False, "error": "作业不存在"}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if ok:
        conn.execute(
            "UPDATE job_jobs SET status='done', progress=100, stage='', result=?, "
            "error='', lease_until='', updated_at=? WHERE id=?",
            (json.dumps(result or {}, ensure_ascii=False, default=str)[:200000],
             now, int(job_id)))
        conn.commit()
        return {"ok": True, "status": "done"}
    attempt = int(job.get("attempt") or 1)
    maxa = int(job.get("max_attempts") or 3)
    msg = (error or "")[:500]
    if attempt < maxa:
        conn.execute(
            "UPDATE job_jobs SET status='queued', error=?, lease_until='', "
            "updated_at=? WHERE id=?", (msg, now, int(job_id)))
        conn.commit()
        return {"ok": True, "status": "queued", "retry": attempt}
    conn.execute(
        "UPDATE job_jobs SET status='failed', error=?, lease_until='', updated_at=? "
        "WHERE id=?", (msg, now, int(job_id)))
    conn.commit()
    return {"ok": True, "status": "failed"}


# ══════════════════════════════════════════════════════════════════
# worker 守护
# ══════════════════════════════════════════════════════════════════
def _run_one(job: dict) -> None:
    """执行单个作业（**在自己的线程里**，调用方负责并发控制）。"""
    kind = job.get("kind") or ""
    fn = _HANDLERS.get(kind)
    conn = _conn()
    try:
        if fn is None:
            report(conn, int(job["id"]), False,
                   error="没有注册 kind='%s' 的处理器（可用：%s）"
                         % (kind, ",".sorted(_HANDLERS))or "无")
            return
        payload = json.loads(job.get("payload") or "{}")
        out = fn(payload, job)
        report(conn, int(job["id"]), True, result=out or {})
    except Exception as e:
        report(conn, int(job["id"]), False,
               error="%s: %s" % (type(e).__name__, str(e)[:400])
                     + (" | " + traceback.format_exc()[-300:] if _cfg("debug", "trace", False) else ""))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_worker_loop(interval_s: float = 2.0, worker_id: str = "w1",
                    lease_s: int = 300, max_idle_rounds: int = 0) -> dict:
    """worker 主循环：领取 → 执行 → 回报，直到 stop 事件被置位。

    :param max_idle_rounds: 连续空闲多少轮后**自行退出**（0=永不自动退出）。
        给一次性脚本用；常驻守护请传0。
    """
    idle = 0
    done = {"done": 0, "failed": 0, "requeued": 0}
    # ⚠️ **首轮必须立即执行**，不能先 `stop.wait(interval)`。
    #   第一版写成 `while not stop.wait(interval): ...`，导致 worker 启动后
    #   要空等一个周期才领第一个作业 —— 提交方看到"作业一直 queued"，
    #   而在短周期测试里甚至会误判成"队列不工作"。
    #   语义：先干一轮活，再进入"干完就等 interval"的循环。
    first = True
    while True:
        if not first and _worker_state["stop"].wait(interval_s):
            break
        first = False
        try:
            conn = _conn()
            try:
                job = pick_next(conn, worker_id=worker_id, lease_s=lease_s)
            finally:
                conn.close()
            if not job:
                idle += 1
                if max_idle_rounds and idle >= max_idle_rounds:
                    break
                continue
            idle = 0
            _run_one(job)
            c2 = _conn()
            try:
                after = (get(c2, int(job["id"])) or {}).get("status")
            finally:
                c2.close()
            if after == "done":
                done["done"] += 1
            elif after == "failed":
                done["failed"] += 1
            elif after == "queued":
                done["requeued"] += 1
        except Exception as _e:
            # worker 绝不能因单次异常而死掉（否则队列静默停摆）。
            # ⚠️ 但**必须留下痕迹**：第一版只 `time.sleep(0.5)` 静默吞掉，
            # 结果作业永远停在 running 而日志里什么都没有 ——
            # 我为此排查了 6 轮才定位（症状：handler 从未被调用）。
            # ⇒ 打印 + 计数（worker_errors），让"队列静默停摆"不可能无感发生。
            done["errors"] = int(done.get("errors", 0)) + 1
            print("[job-worker] 轮次异常（不中断守护）: %s: %s"
                  % (type(_e).__name__, str(_e)[:200]), flush=True)
    return done


def start_workers(count: int = 1, interval_s: float = 2.0, lease_s: int = 300,
                  prefix: str = "job-worker") -> int:
    """启动 N 个后台 worker（幂等）。线程 daemon=True，不阻塞退出。

    为什么默认只1 个：SQLite 是单写者，worker 越多写锁竞争越重。
    真要并行，先量并发下的 `database is locked` 重试率（BaseRepo 已有退避重试）。
    """
    if _worker_state["threads"] and any(t.is_alive() for t in _worker_state["threads"]):
        return 0
    _worker_state["stop"].clear()
    _worker_state["worker_id"] = "%s-%d" % (prefix, int(time.time()) % 10000)
    n = max(1, int(count or 1))
    for i in range(n):
        wid = "%s-%d" % (_worker_state["worker_id"], i + 1)
        t = threading.Thread(target=run_worker_loop,
                             args=(interval_s, wid, lease_s),
                             daemon=True, name="%s-%d" % (prefix, i + 1))
        t.start()
        _worker_state["threads"].append(t)
    return n


def stop_workers() -> None:
    _worker_state["stop"].set()