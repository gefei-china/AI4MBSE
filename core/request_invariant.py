# -*- coding: utf-8 -*-
"""运行时请求重建不变量（P0-1，统一 LLM 入口的一次挂载覆盖所有调用形态）。

对标 DSH ``dsh-agent-loop/lib/invariant.js:14-40``：每次请求派发前，
从**已登记的持久化请求契约**重建期望内容，与实际要发出去的内容逐字段比对，
不一致即判定为漂移。自研工程没有 append-only 会话事件日志，因此采用等价强度的
轻量实现：在 ``LLMClient.chat`` 入口冻结请求生成 ``RequestTicket``（规范化副本 +
sha256 摘要），在真正调用 provider 之前从 ticket 重建并比对。

设计纪律：
  * ``verify_request()`` 是**纯函数**：零 IO、零 LLM、零数据库，可回放。
  * 判定走 ``core/guards.combine_guards`` 的**单调否决**（任一守卫否决即否决，
    不存在放行接口），因此 shadow 模式的「零执行」是可证明的性质。
  * 三态开关 ``request_invariant.mode = off | shadow | enforce``，默认 **shadow**
    （只记录、不阻断），逐字节可回滚；不改任何返回值、不新增生产表。
  * 审计落在内存环形缓冲 + **append-only JSONL 台账**（``logs/request_invariant.jsonl``），
    供诊断接口与门禁读取。2026-11-09 修正：此前只写 ``_LOG.info``，而 root logger
    级别 WARNING 且无 handler ⇒ INFO 事件**从未落盘**，进程重启即丢，
    「积累真实流量、确认无误报后再切 enforce」这一触发条件因此根本无法执行。
    台账写入是**观测设施**：任何失败都被吞掉，绝不变成业务故障。

为什么挂在 ``llm/__init__.py`` 而不是报告里写的 ``stream.py``：
本仓有 stream / execute / orchestration 三条调用形态，逐个堵必然漏。
``LLMClient.chat`` 是它们的唯一公共前躯（该文件 568 行自述「统一 LLM 入口」）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from core import guards
from core.capability_seams import DEFAULT as CAPABILITIES, TriggerPolicy

_LOG = logging.getLogger("mbse.request_invariant")
_EVENTS = deque(maxlen=200)          # 内存环形缓冲：只存判定元数据，不存 prompt 正文
_MAX_CANONICAL = 2_000_000          # 防御性上限：异常大请求不参与深拷贝
_AUDIT_LOCK = threading.Lock()       # 进程内串行化；跨进程靠 O_APPEND 单行写入
ORIGIN_ENV = "MBSE_INVARIANT_ORIGIN"  # runtime（默认）| verify
_DEFAULT_AUDIT_PATH = "logs/request_invariant.jsonl"


def _mode() -> str:
    try:
        from core import config as _cfg
        m = str(_cfg.get("request_invariant", "mode", "shadow") or "shadow").strip().lower()
    except Exception:
        return "shadow"             # 配置设施故障 ⇒ 退回最保守的可观测态，不阻断
    return m if m in ("off", "shadow", "enforce") else "shadow"


def canonical(obj) -> str:
    """确定性序列化：键排序、紧凑分隔、中文不转义，不可序列化对象降级为 str。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RequestTicket:
    digest: str
    messages_json: str
    tools_json: str
    meta_json: str
    created_at: float

    def rebuild(self):
        """从持久化契约重建期望请求（DSH deriveMessages 的等价物）。"""
        return json.loads(self.messages_json), json.loads(self.tools_json)

    def as_dict(self) -> dict:
        return {"digest": self.digest, "created_at": self.created_at,
                "meta": json.loads(self.meta_json)}


def freeze_request(messages, tools=None, meta: dict = None) -> RequestTicket:
    """在统一入口冻结本次请求，生成可重建的持久化契约。"""
    snap_m = copy.deepcopy(messages)
    snap_t = copy.deepcopy(tools)
    mj, tj, meta_j = canonical(snap_m), canonical(snap_t), canonical(meta or {})
    if len(mj) + len(tj) > _MAX_CANONICAL:
        # 超大规模请求只记摘要，避免深拷贝/序列化拖垮主链路
        mj = '{"__oversized__": "%s"}' % _digest(mj)
    return RequestTicket(digest=_digest(mj + "\n" + tj), messages_json=mj,
                         tools_json=tj, meta_json=meta_j, created_at=time.time())


def verify_request(ticket: RequestTicket, messages, tools=None) -> dict:
    """纯函数：重建期望请求并与实际请求逐字节（规范化后）比对。"""
    if ticket is None:
        return {"ok": False, "reason": "缺少 RequestTicket（入口未冻结）"}
    expect_m, expect_t = ticket.rebuild()
    actual_m, actual_t = canonical(messages), canonical(tools)
    ok_m = actual_m == ticket.messages_json
    ok_t = actual_t == ticket.tools_json
    ok = bool(ok_m and ok_t and _digest(actual_m + "\n" + actual_t) == ticket.digest)
    return {"ok": ok, "messages_match": ok_m, "tools_match": ok_t,
            "expected_digest": ticket.digest,
            "actual_digest": _digest(actual_m + "\n" + actual_t),
            "reason": "" if ok else "请求与冻结契约不一致（运行时拼接漂移）",
            "rebuilt_from": {"messages": expect_m if ok else None,
                             "tools": expect_t if ok else None}}


class RequestInvariantError(RuntimeError):
    """enforce 模式下的请求漂移硬失败。"""


def dispatch_guards(ticket: RequestTicket, messages, tools):
    """派发前的守卫集合：全部是同步纯函数，不含任何 IO。"""
    def _reconstruction(_ctx):
        r = verify_request(ticket, messages, tools)
        return None if r["ok"] else r["reason"]

    def _serializable(_ctx):
        try:
            canonical(messages)
            canonical(tools)
            return None
        except Exception as exc:
            return "请求不可确定性序列化: %s" % type(exc).__name__

    return [_reconstruction, _serializable]


def ticket_scope(ticket: RequestTicket) -> dict:
    """从已冻结契约里取**判据坐标系**（run / trace / 会话 / 子任务）。

    ★ 2026-11-10（run 维度下沉）：不变量事件此前只有 mode/allowed/reasons/digest，
    答不了「这条漂移属于哪个编排批次」。旁证：`tool_call_logs.run_id` 自 2026-10-27
    起已 100% 填充，而本台账没有对应字段 ⇒ 两套观测无法按 run 合并。
    ⚠️ 纯函数、零 IO：meta 缺失/损坏时返回全空，**不猜也不补**（宁可缺，不可编）。
    """
    out = {"run_id": 0, "trace_id": "", "conversation_id": 0, "sub_task_key": ""}
    if ticket is None:
        return out
    try:
        meta = json.loads(ticket.meta_json or "{}")
    except Exception:
        return out
    if not isinstance(meta, dict):
        return out
    try:
        out["run_id"] = int(meta.get("run_id") or 0)
    except (TypeError, ValueError):
        out["run_id"] = 0
    for k in ("trace_id", "sub_task_key"):
        v = meta.get(k)
        if isinstance(v, str) and v:
            out[k] = v
    try:
        out["conversation_id"] = int(meta.get("conversation_id") or 0)
    except (TypeError, ValueError):
        out["conversation_id"] = 0
    return out


def assert_before_dispatch(ticket: RequestTicket, messages, tools=None, *,
                           mode: str = None, extra_guards=()) -> dict:
    """在真正调用 provider 之前执行不变量判定。

    shadow：记录 ``would_block`` 但**不抛错、不改返回值**；
    enforce：判定不通过即抛 ``RequestInvariantError``；
    off：完全短路。
    """
    m = mode or _mode()
    if m == "off":
        return {"checked": False, "mode": "off"}
    verdict = guards.combine_guards(
        list(dispatch_guards(ticket, messages, tools)) + list(extra_guards or ()), {})
    event = {"ts": time.time(), "mode": m, "origin": _origin(),
             "allowed": verdict.allowed,
             "reasons": list(verdict.reasons), "digest": ticket.digest if ticket else "",
             "would_block": not verdict.allowed}
    # ★ run 维度：事件带批次坐标系（键名与 tool_call_logs / context_audit 对齐）
    event.update(ticket_scope(ticket))
    _record(event)
    if not verdict.allowed and m == "enforce":
        raise RequestInvariantError("请求重建不变量失败：" + verdict.reason)
    return {"checked": True, "mode": m, "allowed": verdict.allowed,
            "would_block": not verdict.allowed, "blocked": (m == "enforce" and not verdict.allowed),
            "reasons": list(verdict.reasons), "run_id": event.get("run_id", 0),
            "trace_id": event.get("trace_id", "")}


def _audit_config() -> tuple:
    """台账配置：全部走 core.config 单一真源；配置设施故障 ⇒ 关闭观测，不阻断业务。"""
    try:
        from core import config as _cfg
        enabled = bool(_cfg.get("request_invariant", "audit_enabled", True))
        path = str(_cfg.get("request_invariant", "audit_path",
                            "logs/request_invariant.jsonl") or "")
        try:
            limit = int(_cfg.get("request_invariant", "audit_max_bytes",
                                 8 * 1024 * 1024) or 0)
        except (TypeError, ValueError):
            limit = 8 * 1024 * 1024
        return enabled, path, max(limit, 0)
    except Exception:
        return False, "", 0


def _origin() -> str:
    """事件来源：runtime（真实流量）| verify（门禁/验证脚本）。

    为什么必须有这个字段：2026-11-09 实测发现，pr 层门禁与债务探针会真实调用
    ``assert_before_dispatch``，默认又写生产台账 ⇒ CI 每跑一次就往生产台账灌
    若干条 shadow 事件。若 `enforce_readiness` 把它们当真实流量，凑够样本量即
    **假 READY**，切 enforce 的判据彻底失效。来源由执行上下文声明，不靠猜测。
    """
    v = str(os.environ.get(ORIGIN_ENV, "") or "").strip().lower()
    return v if v in ("runtime", "verify") else "runtime"


def audit_path() -> str:
    """当前生效的台账绝对路径（相对路径按工程根解析）。"""
    try:
        from core import config as _cfg
        root = str(getattr(_cfg, "BASE_DIR", "") or os.getcwd())
    except Exception:
        root = os.getcwd()
    _enabled, path, _limit = _audit_config()
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(root, path))


def _rotate_if_needed(path: str, limit: int) -> None:
    """超过上限时轮转一代（os.replace 覆盖，不做删除 —— 见 core/fs_guard.py）。"""
    if limit <= 0:
        return
    try:
        if os.path.isfile(path) and os.path.getsize(path) >= limit:
            os.replace(path, path + ".1")
    except Exception:
        pass


def _append_audit(event: dict) -> bool:
    """append-only 单行写入。观测设施：任何异常都吞掉，绝不变成业务故障。

    **验证上下文不写生产台账**：``origin=verify`` 且路径仍是生产默认值时跳过；
    需要落盘的验证必须显式把 ``audit_path`` 指到临时路径（本仓门禁都这么做）。
    """
    enabled, raw_path, limit = _audit_config()
    if not enabled or not raw_path:
        return False
    if _origin() == "verify" and os.path.normcase(raw_path.replace("\\", "/")) == \
            os.path.normcase(_DEFAULT_AUDIT_PATH):
        return False
    path = audit_path()
    if not path:
        return False
    try:
        with _AUDIT_LOCK:
            _rotate_if_needed(path, limit)
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            line = json.dumps({k: v for k, v in event.items()
                               if k in ("ts", "mode", "origin", "allowed", "reasons",
                                        "digest", "would_block")},
                              ensure_ascii=False, sort_keys=True) + "\n"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        return True
    except Exception:
        return False


def _record(event: dict) -> None:
    _EVENTS.append(event)
    # 结构化日志保留（有 handler 时可见），但不再把它当作持久化依据。
    _LOG.info("request_invariant mode=%s allowed=%s digest=%s reasons=%s",
              event["mode"], event["allowed"], event["digest"][:12],
              ";".join(event["reasons"])[:200])
    _append_audit(event)


def events() -> list:
    return list(_EVENTS)


def reset_events() -> None:
    _EVENTS.clear()


def read_events(path: str = None, limit: int = 5000) -> list:
    """读取持久化台账（用于诊断接口与门禁）。坏行跳过，不因单点损坏整体失败。"""
    p = path or audit_path()
    if not p or not os.path.isfile(p):
        return []
    out = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except Exception:
        return out
    return out[-limit:] if limit and limit > 0 else out


def enforce_readiness(path: str = None, min_samples: int = 200) -> dict:
    """把「shadow 确认无误报后才切 enforce」变成机器可判据（**不自动切换**）。

    只看**真实流量**：``origin='runtime'`` 的 shadow 事件才计样本；门禁/验证脚本
    产生的 ``origin='verify'`` 事件单独报数、不进判据（否则 CI 跑几次就假 READY）。
    判据是「零误报且有足够样本」，不是「跑过就算」；台账为空时明确 not ready。
    """
    evs = read_events(path=path, limit=0)
    by_origin = {}
    for e in evs:
        k = str(e.get("origin") or "unknown")   # 老事件无 origin 字段 ⇒ unknown，不计入样本
        by_origin[k] = by_origin.get(k, 0) + 1
    runtime = [e for e in evs if str(e.get("origin")) == "runtime"
               and str(e.get("mode")) == "shadow"]
    blocked = [e for e in runtime if e.get("would_block") is True]
    modes = {}
    for e in runtime:
        k = str(e.get("mode") or "unknown")
        modes[k] = modes.get(k, 0) + 1
    n = len(runtime)
    return {"samples": n, "total_events": len(evs), "would_block": len(blocked),
            "by_mode": modes, "by_origin": by_origin, "min_samples": int(min_samples),
            "ready": bool(n >= int(min_samples) and not blocked),
            "reason": ("真实流量样本不足（%d < %d）" % (n, int(min_samples))
                       if n < int(min_samples)
                       else ("存在 %d 条 would_block 事件，须先定位" % len(blocked)
                             if blocked else "真实流量样本充分且零误报"))}


class _Capability:
    name = "request_invariant"

    def describe(self) -> dict:
        return {"name": self.name, "layer": "invariant", "side_effects": False,
                "description": "从冻结契约重建并比对 LLM 请求"}


class _Executor:
    def run(self, capability, payload: dict) -> dict:
        r = verify_request(payload["ticket"], payload["messages"], payload.get("tools"))
        return {"ok": r["ok"], "reason": r["reason"], "digest": r["expected_digest"]}


class _ModePolicy(TriggerPolicy):
    def should_run(self, capability, payload) -> bool:
        return _mode() != "off"


# 真实消费 P1-1 的三层 seam：契约 / 执行器 / 触发策略各自独立。
CAPABILITIES.register("request_invariant", _Capability(), _Executor(), _ModePolicy(),
                      replace=True)

__all__ = ["RequestTicket", "RequestInvariantError", "canonical", "freeze_request",
           "verify_request", "dispatch_guards", "assert_before_dispatch",
           "events", "reset_events", "read_events", "audit_path",
           "enforce_readiness", "_mode", "ORIGIN_ENV"]
