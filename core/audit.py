"""跨模块公共工具（原 main.py 通用工具区）：审计日志 + SQLite 行转换。

从 main.py 抽出，供所有 routers 复用；本模块只依赖 database 基础设施。

2026-10-03 P0-a / P0-b（《运行监控与审计日志差距评估》）：
- P0-a 请求溯源：ip_address / user_agent / request_id —— 此前 ip_address 列在 schema 里
  存在，但 audit() 的 INSERT 只写 5 列、从不落盘（审计六要素缺 Source）。
  实现走 ContextVar：HTTP 中间件在请求入口 bind_request()，audit() 自动取用 ——
  避免在 200+ 个调用点上逐个加参数（改不全就等于没改）。
- P0-b 审计哈希链：prev_hash / hash —— 任一历史行被篡改或删除可被检出（NIST AU-9 / SOC2 CC7.2）。
  摘要计算的入参**不含自增 id 与 created_at**，因此 INSERT 前即可算出本行摘要；
  删除/插入会断开 prev_hash 衔接，改内容会破坏本行 hash 重算一致性。
"""
import hashlib
import secrets
import threading
from contextvars import ContextVar

from database import db_conn

# 请求溯源上下文：每请求由 HTTP 中间件绑定（core.middleware）；后台线程为默认值 None。
# 用 ContextVar 而非全局量 —— asyncio 任务切换不会串号。
_req_ctx: ContextVar = ContextVar("mbse_req_ctx", default=None)

# 哈希链写入串行化：进程内串行 + SQLite 单写者，避免两笔并发审计读到同一个 prev_hash 造成链分叉
_chain_lock = threading.RLock()

_GENESIS = "0" * 64   # 创世 prev_hash（链首）


def rows_to_list(rows):
    """sqlite3.Row 列表 → dict 列表（JSON 序列化友好）。

    repositories/base.py 依赖它做行转换 —— 重写本模块时曾一度丢失，导致服务
    启动即 ImportError（教训：动公共模块前先 grep 全库引用点）。
    """
    return [dict(r) for r in rows]

# 摘要计算字段顺序固定为：prev_hash|user_name|event_type|detail|result|branch|ip_address|
# user_agent|request_id（见 _row_hash）—— 新增字段必须追加到末尾，否则历史行摘要全部失效。


def bind_request(request=None) -> dict:
    """绑定当前请求的溯源上下文（IP / User-Agent / request_id）。

    - 由 HTTP 中间件在请求入口调用；返回本次请求的 ctx（含 response 可回写的 request_id）
    - 传 None 表示清理（请求结束 / 后台任务线程无 HTTP 语境）
    """
    if request is None:
        _req_ctx.set(None)
        return {}
    headers = request.headers
    # X-Forwarded-For 由反向代理追加（"client, proxy1, proxy2"），第一跳才是真实来源 IP
    ip = (headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if getattr(request, "client", None) else "")
          or "")
    ctx = {
        "ip": ip,
        "user_agent": (headers.get("user-agent") or "")[:300],
        "request_id": headers.get("x-request-id") or secrets.token_hex(8),
    }
    _req_ctx.set(ctx)
    return ctx


def request_context() -> dict:
    """当前请求的溯源上下文（无 HTTP 语境时返回空 dict）。"""
    return _req_ctx.get() or {}


# ── P0-c（2026-10-03）链路上下文：LLM 调用的 trace→span 归属 ──
# 与请求溯源分开：请求溯源答"谁从哪来"，链路上下文答"这次调用属于哪次会话/哪个 run"。
# 同样走 ContextVar —— 同步调用链天然继承，线程池处显式 copy_context（见 stream.py 的
# _submit_with_ctx）；避免给几十个 LLM 调用点逐个加参数（改不全等于没改）。
_trace_ctx: ContextVar = ContextVar("mbse_trace_ctx", default={})

_TRACE_KEYS = ("conversation_id", "run_id", "trace_id", "sub_task_key")


def bind_trace(**kw) -> dict:
    """合并设置链路上下文字段（conversation_id / run_id / trace_id / sub_task_key）。

    空戴 semantics：0 / '' 表示"不属于"，传 None 表示"不清该字段"。返回合并后的完整上下文。
    """
    cur = dict(_trace_ctx.get() or {})
    for k, v in kw.items():
        if k in _TRACE_KEYS and v is not None:
            cur[k] = v
    _trace_ctx.set(cur)
    return cur


def trace_context() -> dict:
    return _trace_ctx.get() or {}


def clear_trace() -> None:
    _trace_ctx.set({})


def begin_trace(conversation_id=0, **kw) -> dict:
    """P0-c：流水线入口开启一次链路 —— 自动沿用当前请求的 request_id 作 trace_id。

    同一请求内的多次 LLM 调用（意图识别 / 主生成 / 编排汇总 …）共享同一个 trace_id，
    于是可以按 trace 聚合出「这一轮到底花了多少 token / 多少钱」。
    """
    _tid = kw.pop("trace_id", None) or request_context().get("request_id") or secrets.token_hex(8)
    return bind_trace(conversation_id=int(conversation_id or 0), trace_id=_tid, **kw)


def _row_hash(prev_hash: str, user, event, detail, result, branch, ip, ua, rid) -> str:
    """单行摘要：sha256(prev_hash|各字段)，字段以 \\x1f 分隔避免拼接歧义。"""
    parts = [prev_hash or _GENESIS, str(user or ""), str(event or ""), str(detail or ""),
             str(result or ""), str(branch or ""), str(ip or ""), str(ua or ""), str(rid or "")]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _last_hash(conn) -> str:
    """链尾摘要（按 id 降序第一行有 hash 的记录）。"""
    row = conn.execute(
        "SELECT hash FROM audit_logs WHERE hash IS NOT NULL AND hash<>'' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    return (row["hash"] if row else None) or _GENESIS


def audit(user, event, detail, result="success", conn=None, branch=""):
    """写入审计日志（统一审计口径 + 请求溯源 + 哈希链）。

    - conn 传入（推荐，P2 后路由持有请求级连接）：审计与业务写
      在同一事务提交，避免 SQLite 写锁冲突（database is locked）。
    - conn 缺省：独立开连接即时提交（兼容旧调用方）。
    - branch：分支归属（2026-09-14）——图谱/推理等域事件记录操作分支，
      供历史 Tab / 审计查询按分支过滤；全局事件（登录/LLM 等）留空。
    - user：审计归属；None/空 时落「未登录」，不要写死人名（可信性）。
    """
    ctx = request_context()
    ip, ua, rid = ctx.get("ip", ""), ctx.get("user_agent", ""), ctx.get("request_id", "")
    user = user or "未登录"

    sql = ("INSERT INTO audit_logs (user_name, event_type, detail, result, branch, "
           "ip_address, user_agent, request_id, prev_hash, hash) "
           "VALUES (?,?,?,?,?,?,?,?,?,?)")

    def _write(c):
        # 哈希链：先取链尾再算本行摘要，全程持锁（进程内写串行）
        with _chain_lock:
            prev = _last_hash(c)
            h = _row_hash(prev, user, event, detail, result, branch or "", ip, ua, rid)
            c.execute(sql, (user, event, detail, result, branch or "", ip, ua, rid, prev, h))
            return h

    def _write_degrade(c):
        """降级写入（列迁移未跑到时不阻断业务）——保持与原实现相同的 5 列。"""
        c.execute("INSERT INTO audit_logs (user_name, event_type, detail, result, branch) "
                  "VALUES (?,?,?,?,?)", (user, event, detail, result, branch or ""))
        return ""

    _MISSING_COL_HINTS = ("no such column", "has no column", "no column named")

    def _safe(c):
        try:
            return _write(c)
        except Exception as _e:
            # 列迁移未跑到时降级写入，绝不让审计失败阻断业务。
            # 措辞必须覆盖 SQLite 真实报错："no such column: hash"（写侧）、
            # "table audit_logs has no column named hash"（旧版本）。写窄了降级就成了空头支票。
            if any(h in str(_e).lower() for h in _MISSING_COL_HINTS):
                return _write_degrade(c)
            raise

    if conn is not None:
        return _safe(conn)
    with db_conn() as c:
        return _safe(c)


def audit_user(user) -> str:
    """审计归属：当前登录用户显示名；未识别请求标记为「未登录」。

    user 为 core.deps.current_user 依赖返回值（dict | None）。
    替代历史写死的 audit("admin"/"王工", ...) 调用，使审计日志可信。
    """
    return (user or {}).get("display_name") or (user or {}).get("username") or "未登录"


def verify_chain(limit: int = 0, conn=None) -> dict:
    """校验审计链完整性：逐行重算摘要 + 校验 prev_hash 衔接。

    返回 {total, chained, unchained, broken:[{id,reason}], ok}
    - tampered：内容与摘要不符（行被改过）
    - head_next_mismatch：prev_hash 与上一行 hash 不符（行被插入或删除）
    - unchained：hash 为空（未经 audit() 写入 / 迁移未覆盖到）
    """
    def _check(c):
        where = "" if not limit else f"WHERE id > (SELECT MAX(id)-{int(limit)} FROM audit_logs)"
        rows = c.execute(
            f"SELECT id, user_name, event_type, detail, result, branch, ip_address, "
            f"user_agent, request_id, prev_hash, hash FROM audit_logs {where} ORDER BY id").fetchall()
        total, chained, broken, prev = len(rows), 0, [], _GENESIS
        for r in rows:
            h, ph = r["hash"] or "", r["prev_hash"] or ""
            if not h:
                continue                      # 未入链（旧数据/绕过写入），不计 chained
            chained += 1
            if ph != prev:
                broken.append({"id": r["id"], "reason": "prev_hash 不衔接（行被插入或删除）",
                               "expected": prev[:12], "actual": ph[:12]})
            expect = _row_hash(ph, r["user_name"], r["event_type"], r["detail"], r["result"],
                               r["branch"] or "", r["ip_address"] or "", r["user_agent"] or "",
                               r["request_id"] or "")
            if expect != h:
                broken.append({"id": r["id"], "reason": "内容与摘要不符（行被篡改）"})
            prev = h
        return {"total": total, "chained": chained, "unchained": total - chained,
                "broken": broken[:50], "broken_count": len(broken), "ok": not broken}

    if conn is not None:
        return _check(conn)
    with db_conn() as c:
        return _check(c)
