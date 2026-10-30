# -*- coding: utf-8 -*-
"""写类工具幂等键（2026-10-30）—— durable execution 的第一步。

━━━ 为什么需要这个模块 ━━━
《引擎选型评估与控制面收敛-20261030》§2.2 的核心结论：**引擎不会替你做幂等**。
行业原话（LangGraph / Temporal / DBOS 官方口径一致）：

    "Every engine here behaves the same way for an unfinished step,
     so pass an idempotency key to anything with side effects."

⇒ 换引擎之前，这把钥匙必须先由我们自己造出来；造出来之后，将来迁引擎
   只是"把实现换成引擎的 step 机制"，而不是"重写系统"。
   这也是 LangGraph 官方 "Retrofit 三步法" 的第 1 步
   （① 幂等键 → ② 任务图外部化 → ③ 才把循环搬进引擎）。

━━━ 本工程的具体缺口（不是理论风险）━━━
- `agent_tasks.retry_count` 的重试是**同 key 原地重派**（`UPDATE agent_tasks`）；
- `orch_checkpoint.prepare_resume` 的续跑是**沿用原批次号**重派未完成任务；
⇒ 同一个子任务里的写类工具调用会被**执行两次**：第一次（可能已落库）、
  第二次（重试/续跑时）⇒ **重复写入**。
实测口径：本仓 `agent_tasks` 76 条失败里 63 条是超时（83%），
超时后重试/续跑必然重跑该子任务里的写工具 ⇒ 这是会真实发生的路径。

━━━ 幂等键的构造（四段，缺一不可）━━━
    key = sha256(scope | tool | args_hash | ordinal)

1. `scope` —— **执行单元**：`run:<批次号>:task:<task_key>`。
   编排重试/续跑时批次号与 task_key 都**不变** ⇒ 键不变 ⇒ 命中。
   ⚠️ 非编排路径（会话直答）**没有"同一步被重跑"的语义** ⇒ scope 为空 ⇒ 不参与幂等。
   （不把"同一会话里用户第二次说同样的话"误判成重放 —— 那是两次不同的意图。）

2. `tool` —— 工具名。

3. `args_hash` —— 规范化参数的摘要（key 排序、去空值、路径分隔归一）。

4. `ordinal` —— **同一次尝试内**该 `(tool, args)` 组合的第几次出现。
   为什么必须有它：`file_append` 同一份内容连写两次是**两个不同的意图**，
   不能被吞掉；而重试时整个子任务从头再跑 ⇒ 序号**重新从 0 开始**
   ⇒ 第 N 次调用与上一次尝试的第 N 次调用对齐 ⇒ 正好命中。
   ⚠️ 序号挂在**执行单元实例**上（编排子任务每次尝试都新建 `sub` 实例
   ⇒ 序号天然归零），**不能**挂全局计数器（全局计数器会让重试永不对齐）。

━━━ 记录什么、不记录什么（这条决定了会不会吞掉合法写入）━━━
只记录**真正执行成功**（`ok=True`）的写类调用：
- 被白名单 / RBAC / Hook / 写前确认(HITL) / 写意图闸**拦下**的结果**不记录**
  ⇒ 它们不是"已产生副作用"，下一轮该拦还拦
  （否则人工确认队列会被"重放"吃掉 —— 那才是真事故）。
- 失败（`ok=False`）也不记录 ⇒ 允许重试（重试前是否已部分落库，本模块无从得知；
  这是**已知局限**，见文件尾「局限声明」）。

━━━ 失败开放（fail-open）━━━
本模块处在工具执行入口，**任何异常都不得阻断写操作**：
存储读不到 / 写失败 / 表不存在 ⇒ 一律按"没有历史记录"处理，照常执行。
（反过来的 fail-closed 会让幂等表故障 = 全平台写操作瘫痪。）
"""
import hashlib
import json
import os
import threading
import time

__all__ = [
    "enabled", "classify_write", "derive_scope", "args_hash", "derive_key",
    "prepare", "lookup", "record", "replay", "prune", "stats",
]

#: 序号计数器锁（同一执行单元内可能被多个线程并发调用工具）
_SEQ_LOCK = threading.Lock()

#: 幂等键长度（sha256 前 32 位 hex = 128 bit，碰撞概率对本场景可忽略）
_KEY_LEN = 32


# ── 配置 ──────────────────────────────────────────────────────────
def _cfg(group, key, default):
    """读配置项；**任何异常都退回默认值**（入口路径不能让配置故障变成写操作故障）。"""
    try:
        from core import config as _c
        v = _c.get(group, key, default)
        return default if v is None else v
    except Exception:
        return default


def enabled():
    """幂等闸总开关 + 作用域开关。

    `runtime.tool_idempotency_scope`:
      - ``"subtask"``（默认）：只在**编排子任务**范围内生效（有重试/续跑语义的地方）；
      - ``"off"``：完全关闭（回退位）。
    """
    if not bool(_cfg("runtime", "tool_idempotency_enabled", True)):
        return False
    return str(_cfg("runtime", "tool_idempotency_scope", "subtask")).lower() != "off"


def classify_write(name, side_effect=""):
    """写类判定 —— **与写前确认闸同源**（`hil_service.classify_action` 双源口径）。

    为什么要同源：写前确认闸已经解决了"MCP 工具不在 `tools` 表 ⇒ side_effect 恒判 read
    ⇒ 三道闸全失效"这个实测断链。幂等是**第四道闸**，若另起一套判定口径，
    就会出现"确认闸认为是写、幂等认为是读"⇒ 写操作被重复执行。
    ⇒ **同一语义只写一份**：直接复用，不复制判据。

    :return: `(is_write, kind)`；kind ∈ read / write / destructive
    """
    try:
        from hil_service import classify_action as _ca
        r = _ca(name, side_effect or "") or {}
        kind = str(r.get("kind") or "read")
        return kind in ("write", "destructive"), kind
    except Exception:
        return False, "read"


# ── 作用域 ────────────────────────────────────────────────────────
def derive_scope(pipeline):
    """执行单元标识。

    - 编排子任务：`run:<批次号>:task:<task_key>`
      （`stream.py` 在派发子任务时设 `sub._tool_run_ctx` / `sub._tool_task_key`；
        重试与续跑都**沿用同一批次号与 task_key** ⇒ 键稳定 ⇒ 命中）
    - 其它一切路径：返回 `""` ⇒ **不参与幂等**（照常执行，不查不写）。
    """
    if str(_cfg("runtime", "tool_idempotency_scope", "subtask")).lower() == "off":
        return ""
    try:
        run_id = int(getattr(pipeline, "_tool_run_ctx", 0) or 0)
        task_key = str(getattr(pipeline, "_tool_task_key", "") or "")
        if run_id and task_key:
            return "run:%d:task:%s" % (run_id, task_key)
    except Exception:
        return ""
    return ""


# ── 键推导 ────────────────────────────────────────────────────────
def canonical_args(args):
    """参数归一化（**只用于算摘要，不改变实际执行参数**）。

    规则：
      - 丢弃 `None` 与空串（"没传"与"传了空串"在写操作上语义相同）；
      - 路径分隔归一（`a\\b` 与 `a/b` 在 Windows 上是同一路径 ⇒ 不该产生两个键）；
      - 其余类型转字符串（保证 JSON 可序列化且稳定）。
    """
    if not isinstance(args, dict):
        return {"_v": str(args)}
    out = {}
    for k, v in args.items():
        if v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
            if not v:
                continue
            if "\\" in v or "/" in v:
                v = v.replace("\\", "/")
                while len(v) > 1 and v.endswith("/"):
                    v = v[:-1]
        elif isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (int, float)):
            pass
        elif isinstance(v, (list, tuple)):
            v = [str(x) for x in v]
        else:
            v = str(v)
        out[str(k)] = v
    return out


def args_hash(args):
    """参数摘要（24 位 hex）。"""
    try:
        blob = json.dumps(canonical_args(args), sort_keys=True,
                          ensure_ascii=False, default=str)
    except Exception:
        blob = str(args)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def derive_key(tool, scope, ahash, ordinal):
    """幂等键 = sha256(scope|tool|args_hash|ordinal)[:32]。"""
    raw = "%s|%s|%s|%s" % (scope or "", tool or "", ahash or "", int(ordinal or 0))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_KEY_LEN]


def _next_ordinal(pipeline, tool, ahash):
    """同一次尝试内 `(tool, args_hash)` 的第几次出现（从 0 开始）。

    计数挂在**执行单元实例**上 ⇒ 子任务每次重试都新建实例 ⇒ 序号天然归零
    ⇒ 重试时的第 N 次调用与上一次尝试的第 N 次调用对齐（这正是我们要的）。

    ⚠️ 为什么不是"全局调用序号"：模型在重试时可能多调/少调一次**别的**工具，
    全局序号会整体错位 ⇒ 幂等永远不命中（静默失效）。
    """
    with _SEQ_LOCK:
        m = getattr(pipeline, "_tool_idem_seq", None)
        if not isinstance(m, dict):
            m = {}
            try:
                pipeline._tool_idem_seq = m
            except Exception:
                pass
        k = "%s|%s" % (tool or "", ahash or "")
        n = int(m.get(k, 0) or 0)
        m[k] = n + 1
    return n


def prepare(pipeline, name, arguments, side_effect=""):
    """工具执行前的幂等闸准备。返回幂等上下文 dict 或 `None`（不适用）。

    返回 dict：`{"key", "scope", "args_hash", "ordinal"}` —— `ordinal` 一并带出，
    便于执行后**原样**记账（记错序号会让"重试对齐"静默失效）。

    `None` 的三种情形（都意味着"照常执行，不查不写"）：
      1. 幂等闸关闭 / 作用域设为 off；
      2. 不在编排子任务作用域内（scope 为空）；
      3. 该工具被判为只读（非写类）。
    """
    if not enabled():
        return None
    scope = derive_scope(pipeline)
    if not scope:
        return None
    is_write, _kind = classify_write(name, side_effect)
    if not is_write:
        return None
    ah = args_hash(arguments)
    ordinal = _next_ordinal(pipeline, name, ah)
    return {
        "key": derive_key(name, scope, ah, ordinal),
        "scope": scope,
        "args_hash": ah,
        "ordinal": ordinal,
    }


# ── 存储 ──────────────────────────────────────────────────────────
def _table_ready(conn):
    try:
        r = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_idempotency'"
        ).fetchone()
        return bool(r)
    except Exception:
        return False


def lookup(key):
    """查历史记录。返回 dict 或 `None`（无记录 / 表不存在 / 任何异常）。"""
    if not key:
        return None
    try:
        from database import get_db
        conn = get_db()
        try:
            if not _table_ready(conn):
                return None
            row = conn.execute(
                "SELECT key, tool, scope, args_hash, ordinal, result_json, hits, created_at "
                "FROM tool_idempotency WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def record(key, tool, scope, ahash, ordinal, result):
    """记录一次**成功执行**的写类调用。返回 True/False。

    ⚠️ 只在 `result["ok"] is True` 时调用（由调用方判定）——
       被闸拦下/失败的结果**不得**记录（否则会把"没执行"当成"执行过"）。
    """
    if not key:
        return False
    try:
        blob = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return False
    try:
        from database import db_conn
        with db_conn() as conn:
            if not _table_ready(conn):
                return False
            conn.execute(
                "INSERT INTO tool_idempotency "
                "(key, tool, scope, args_hash, ordinal, result_json, hits, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET result_json=excluded.result_json, "
                "updated_at=CURRENT_TIMESTAMP",
                (key, tool or "", scope or "", ahash or "", int(ordinal or 0), blob))
        return True
    except Exception:
        return False


_REPLAY_NOTE = ("\n（幂等重放：同一子任务的上一次尝试/续跑中，该写操作已成功执行；"
                "此处直接复用其结果，**未重复产生副作用**。）")


def replay(row):
    """把历史记录还原成工具结果（附重放标记）。

    为什么要**如实告诉模型**：否则模型会把"重放"当成"新执行成功"，
    继续往下推演（例如再写一次），反而放大重复写入。
    """
    if not row:
        return None
    try:
        res = json.loads(row.get("result_json") or "{}")
    except Exception:
        return None
    if not isinstance(res, dict):
        return None
    res["idempotent_replay"] = True
    r = res.get("result")
    if isinstance(r, str):
        res["result"] = r + _REPLAY_NOTE
    elif res.get("ok") is True and "result" not in res:
        res["result"] = _REPLAY_NOTE.strip()
    try:
        from database import db_conn
        with db_conn() as conn:
            conn.execute("UPDATE tool_idempotency SET hits=COALESCE(hits,0)+1, "
                         "updated_at=CURRENT_TIMESTAMP WHERE key=?", (row.get("key"),))
    except Exception:
        pass
    return res


def prune(ttl_s=None):
    """清理过期记录（TTL 默认 24h）。返回删除行数。

    ⚠️ `created_at` 是 SQLite `CURRENT_TIMESTAMP` = **UTC**，
       `datetime('now')` 同为 UTC ⇒ 口径一致（别用本地时间比）。
    """
    ttl = int(ttl_s if ttl_s is not None else _cfg("runtime", "tool_idempotency_ttl_s", 86400))
    if ttl <= 0:
        return 0
    try:
        from database import db_conn
        with db_conn() as conn:
            if not _table_ready(conn):
                return 0
            cur = conn.execute(
                "DELETE FROM tool_idempotency WHERE created_at < datetime('now', ?)",
                ("-%d seconds" % ttl,))
            return int(cur.rowcount or 0)
    except Exception:
        return 0


def stats():
    """观测快照（供 /api/monitor 与门禁脚本）。"""
    out = {"enabled": enabled(), "scope": _cfg("runtime", "tool_idempotency_scope", "subtask"),
           "ttl_s": int(_cfg("runtime", "tool_idempotency_ttl_s", 86400) or 0),
           "total": 0, "replays": 0}
    try:
        from database import get_db
        conn = get_db()
        try:
            if not _table_ready(conn):
                return out
            r = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(hits),0) h "
                              "FROM tool_idempotency").fetchone()
            out["total"] = int(r["c"] or 0)
            out["replays"] = int(r["h"] or 0)
        finally:
            conn.close()
    except Exception:
        pass
    return out


# ── 局限声明（如实登记，别在文档里夸大覆盖面）────────────────────────
# ① 只覆盖**编排子任务**路径（scope 非空）。会话直答路径没有"同一步重跑"语义，
#    故不纳入 —— 避免把"用户第二次说同样的话"误判成重放。
# ② 只覆盖**写类**工具（按 `hil_service.classify_action` 双源判定）。
#    只读工具重复执行无副作用，不纳入（省一次 DB 往返）。
# ③ **写操作"部分成功"的情形无法识别**：工具返回 `ok=False` 但实际已落库时，
#    本模块不记录 ⇒ 重试会重复执行。这是本方案的已知边界
#    （彻底解决需要工具自身返回"已写入目标 id"这类可校验凭据，属另一条线）。
# ④ **记账用独立连接**（`db_conn()`），与被记账工具自己的事务不共享。
#    若工具"返回成功但事务随后回滚"，会出现"记账了、副作用没落" ⇒ 重试时被重放
#    ⇒ 表现为"这次什么都没做"。本仓工具的约定是**自己提交**（`job_handlers` /
#    `file_tools` / MCP 客户端均如此），故实际风险很窄；
#    但它**不是零**，一旦出现症状（重试后目标端没有新数据），先查这一点。
# ⑤ 幂等表与业务库同库（SQLite 单写者）⇒ 写工具本身已在写库 ⇒ 不增加新的写放大。
#    但 `prune()` 会周期删除旧行，属额外的写操作（默认手动/脚本调用，不在热路径）。
