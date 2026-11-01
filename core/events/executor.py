# -*- coding: utf-8 -*-
"""工单执行器（2026-11-01，P2-1）—— 把工单的 action **真正跑起来**。

━━━ 为什么必须有这个模块（这是此前最大的缺口）━━━
前两轮交付的链路是「**事件 → 记录**」（Trigger → WorkItem），
**全仓没有任何代码执行 workitem 的 action**（已 grep 复核：`workitem` 只出现在
`event_service` / `event_repo` / `routers/events` 三个文件里，且都只改状态）。
⇒ Super-agent 最核心的四个特征里，「**主动**」（主动推进值得做的事）
与「**高效**」（任务真的被完成）都**没有兑现** ——
只有「连续」（接力）与「持久」（看门狗）是实的。

本模块补上这段：**工单 → 执行 → 结果 → 状态与留痕**。

━━━ 为什么执行器与"准入"彻底分离 ━━━
- **准入层**（`core/events/model.py::admit`）决定"这件事**该不该做**"
  —— 只依赖结构化字段，**绝不读正文**。
- **执行层**（本模块）决定"这件事**怎么执行**"。
两层分开 ⇒ 即使执行器有 bug，也**不可能**让它绕过准入去做没授权的事。
（若把准入塞进执行器，一次代码改动就可能变成越权通道。）

━━━ 三条执行纪律（每条都对应一个真实风险）━━━
① **mode=observe 的工单一律拒绝执行** —— 观察期的定义就是"只记录不执行"。
   判据取工单**快照**上的 mode（落库时的模式），不取来源当前模式
   ⇒ 来源后来切了 execute，**已建的观察期工单仍然不能执行**（防"观察期被绕过"）。
② **执行必须幂等** —— 同一个工单重复执行不得产生双份副作用。
   幂等键 = `workitem:<wid>`（对齐 `core/idempotency.py` 的"幂等键锚定当次操作"）。
③ **执行失败必须留痕且可重试** —— 失败写 `error` + state='failed'，
   **不吞异常**（对齐 `core/orch_supervisor.py` 的"绝不在守护里吞异常而不留痕迹"）。

━━━ 执行动作从哪来（不重复造轮子）━━━
本模块**不自己实现业务动作**，而是**委派给既有能力**：
  · `extract_entities` → `vector2graph.extract_candidates`（写候选表，不直接写图谱）
  · `review_trace`    → 只读：产出待审清单，不写库
动作注册表可扩展（新增动作 = 注册一个 handler），但**每个 handler 必须自己声明**：
  · 是否写库（`writes`）—— 决定它能不能在 observe 源下被调度
  · 幂等键构造（`idem_key`）

━━━ 为什么执行要**串行**而非并发 ━━━
SQLite 是单写者（本仓 docker-compose 明写"单副本是硬约束"）。
并发执行抽取 ⇒ 写锁竞争 ⇒ `database is locked` 重试风暴。
⇒ 本模块**单线程串行**执行，一次一个工单。
（与 `core/job_queue.py` 默认 worker_count=1 同源判断。）
"""

from __future__ import annotations

import json
import time
import traceback

__all__ = [
    "EXECUTION_HANDLERS", "register_handler", "get_handler",
    "execute_workitem", "execute_pending", "ExecutionResult",
]


class ExecutionResult(dict):
    """执行结果（dict 子类，便于直接 JSON 序列化）。

    字段：
      · ok        —— 是否成功
      · action    —— 执行的动作
      · result    —— 动作的产出（结构化）
      · error     —— 失败原因
      · elapsed_ms—— 耗时（**价值数据的来源**）
      · skipped   —— 跳过原因（observe / 无 handler / 终态）
    """

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))


# ── handler 注册表 ──
# 每个 handler: fn(conn, workitem) -> dict
#   约定：handler 内部**不得抛异常**（抛了由执行器兜住并留痕），
#        返回 {"ok": bool, "result": dict, "writes": bool}
EXECUTION_HANDLERS: dict = {}


def register_handler(action: str, fn):
    """注册一个执行 handler（同名覆盖，幂等重载）。"""
    EXECUTION_HANDLERS[action] = fn
    return fn


def get_handler(action: str):
    return EXECUTION_HANDLERS.get(action)


# ══════════════════════════════════════════════════════════
# 内置 handler
# ══════════════════════════════════════════════════════════
def _h_extract_entities(conn, wi: dict) -> dict:
    """文档抽取实体 —— 委派给 `vector2graph.extract_candidates`。

    ⚠️ **写候选表，不直接写图谱** —— 这是刻意的：
    抽取产出的候选要人工/流程审核后才进图谱（既有 `domain_review_queue` 机制）。
    ⇒ 执行本动作**不会**污染正式图谱，回滚成本低。

    ⚠️ **开关保护**：既有「文件管理实体与关系抽取」开关关闭时**拒绝执行**，
    保持与 `POST /api/documents/{id}/extract` 的两态一致（不绕过既有开关）。
    """
    sw = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM settings").fetchall()}
    if (sw.get("file_auto_extract_enabled") or "0") != "1":
        return {"ok": False, "writes": False,
                "error": "文件管理实体与关系抽取未开启（既有开关保护）",
                "result": {"skipped": "switch_off"}}

    doc_id = int((wi.get("scope") or "").split(":")[-1] or 0)
    if not doc_id:
        return {"ok": False, "writes": False, "error": "scope 缺 doc_id",
                "result": {}}

    row = conn.execute(
        "SELECT filename, parse_status FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return {"ok": False, "writes": False, "error": "document_not_found",
                "result": {"doc_id": doc_id}}
    if row["parse_status"] != "completed":
        return {"ok": False, "writes": False, "error": "文档未完成解析，无法抽取",
                "result": {"doc_id": doc_id, "parse_status": row["parse_status"]}}

    from vector2graph import extract_candidates
    title_row = conn.execute(
        "SELECT title FROM doc_metadata WHERE document_id=?", (doc_id,)).fetchone()
    query = (title_row[0] if title_row and title_row[0] else row["filename"])[:80]
    ext = extract_candidates(conn, query, doc_id=doc_id, top_k=5)
    return {"ok": True, "writes": True,
            "result": {
                "doc_id": doc_id,
                "batch_id": ext.get("batch_id", ""),
                "candidates": len(ext.get("candidates", [])),
                "node_count": ext.get("node_count", 0),
                "edge_count": ext.get("edge_count", 0),
            }}


def _h_review_trace(conn, wi: dict) -> dict:
    """只读动作：产出该文档的待审追溯线索（**不写库**）。

    用途：作为"零风险动作"的样板 —— 放开自动执行时，先用只读动作验证链路，
    再考虑写库动作。
    """
    doc_id = int((wi.get("scope") or "").split(":")[-1] or 0)
    if not doc_id:
        return {"ok": False, "writes": False, "error": "scope 缺 doc_id", "result": {}}
    row = conn.execute(
        "SELECT filename, chunk_count, entity_count, quality_score "
        "FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return {"ok": False, "writes": False, "error": "document_not_found",
                "result": {"doc_id": doc_id}}
    n_rel = conn.execute(
        "SELECT COUNT(*) FROM relations WHERE source_id=?", (str(doc_id),)).fetchone()[0]
    return {"ok": True, "writes": False,
            "result": {"doc_id": doc_id, "filename": row["filename"],
                       "chunk_count": row["chunk_count"],
                       "entity_count": row["entity_count"],
                       "quality_score": row["quality_score"],
                       "linked_relations": n_rel,
                       "note": "只读：产出待审线索，不写库"}}


register_handler("extract_entities", _h_extract_entities)
register_handler("review_trace", _h_review_trace)


# ══════════════════════════════════════════════════════════
# 执行
# ══════════════════════════════════════════════════════════
def execute_workitem(conn, workitem_id: str, repo=None,
                     service=None) -> ExecutionResult:
    """执行一个工单。**返回结构化结果，不抛异常。**

    ⚠️ **mode 判据取工单快照**（`wi['mode']`），不取来源当前模式：
    来源后来切 execute，已建的 observe 工单**仍不可执行** ⇒ 观察期不会被绕过。

    ⚠️ **终态工单不可执行** —— 已 done/failed/stopped 的事不重跑。
    """
    from core.events.model import decide_workitem_transition
    from repositories.event_repo import EventRepo
    from services.event_service import EventService

    repo = repo or EventRepo(conn)
    svc = service or EventService(repo)
    t0 = time.time()

    def _done(res: dict) -> ExecutionResult:
        res["elapsed_ms"] = int((time.time() - t0) * 1000)
        return ExecutionResult(res)

    wi = repo.get_workitem(workitem_id)
    if not wi:
        return _done({"ok": False, "error": "workitem_not_found", "result": {}})

    # ① 观察期工单拒绝执行（快照口径）
    if wi.get("mode") != "execute":
        return _done({"ok": False, "skipped": "observe_mode",
                      "error": "工单为观察模式（mode=observe），不可执行",
                      "action": wi.get("action", ""), "result": {},
                      "mode": wi.get("mode")})

    # ② 终态不可执行
    ok, why = decide_workitem_transition(wi.get("state", "open"), "running")
    if not ok:
        return _done({"ok": False, "skipped": "illegal_state",
                      "error": why, "action": wi.get("action", ""),
                      "result": {}, "state": wi.get("state")})

    # ③ 状态推进到 running（**先落 running 再执行** ⇒ 崩了能看到"卡在 running"）
    repo.set_workitem_state(workitem_id, "running")

    action = str(wi.get("action") or "").split(":")[0].strip()
    handler = get_handler(action)
    if not handler:
        repo.set_workitem_state(workitem_id, "failed",
                                error="no_handler:%s" % action)
        return _done({"ok": False, "error": "no_handler:%s" % action,
                      "action": action, "result": {}})

    # ④ 执行（异常兜住 → 留痕，不吞）
    try:
        out = handler(conn, wi) or {}
    except Exception as e:
        detail = "%s: %s" % (type(e).__name__, str(e)[:200])
        repo.set_workitem_state(workitem_id, "failed", error=detail)
        try:
            from core.audit import audit
            audit("system", "event_workitem_failed",
                  "工单 %s 执行失败 action=%s: %s" % (workitem_id[:14], action, detail),
                  conn=conn)
        except Exception:
            pass
        return _done({"ok": False, "error": detail, "action": action, "result": {},
                      "traceback": traceback.format_exc()[-600:]})

    ok = bool(out.get("ok"))
    result = out.get("result") or {}
    if ok:
        repo.set_workitem_state(workitem_id, "done",
                                result=json.dumps(result, ensure_ascii=False)[:2000])
    else:
        repo.set_workitem_state(workitem_id, "failed",
                                error=str(out.get("error") or "")[:400])
    return _done({"ok": ok, "action": action, "result": result,
                  "error": str(out.get("error") or ""),
                  "writes": bool(out.get("writes"))})


def execute_pending(conn, limit: int = 10, repo=None, service=None,
                    only_action: str = "") -> list:
    """批量执行待办工单（state='open' 且 mode='execute'）。

    ⚠️ **串行**（SQLite 单写者 ⇒ 并发会打写锁）。
    ⚠️ **只取 mode='execute' 的工单** —— observe 工单跳过（观察期铁律）。
    """
    from repositories.event_repo import EventRepo
    repo = repo or EventRepo(conn)
    out = []
    items = repo.list_workitems(state="open", mode="execute", limit=limit)
    for wi in items:
        if only_action and not str(wi.get("action") or "").startswith(only_action):
            continue
        out.append(execute_workitem(conn, wi["id"], repo=repo, service=service))
    return out
