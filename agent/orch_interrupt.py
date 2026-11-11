# -*- coding: utf-8 -*-
"""编排「可中断」策略（2026-11-10）—— 用户能在长任务中途叫停，并且**保得住续跑资格**。

## 为什么单开一个模块，而不是复用 `TaskQueue.cancel_run`
两者语义**不同**，混用会把"暂停"变成"销毁"：

| | 取消 cancel | 中断 interrupt |
|---|---|---|
| 意图 | 这批不要了 | 先停下，等我说下一步 |
| checkpoint | `phase='cancelled'` | `phase='interrupted'` |
| 任务行 | `cancelled`（终态） | `cancelled`（终态，但**语义上待重排**）|
| 可恢复 | **否**（`resumable()` 因 pending=0 自然滤掉）| **是**（resume 时把中断型 cancelled 重排队）|
| 留痕 | 无 | `orch_interrupts` 一行（含用户补充指令）|

⇒ 关键判据不是"任务状态"，而是 **checkpoint.phase**：
   `interrupted` 的 cancelled 任务是"暂停"，`cancelled` 的 cancelled 任务是"销毁"。

## 为什么必须"协作式"而不是 kill 线程
子任务线程正持有 LLM 流式连接 / SQLite 事务 / 文件句柄，强杀 = 资源泄漏 + 事务悬挂
（本仓 2026-10-14 已踩过，见 `TaskQueue.is_cancel_requested` 的注释）。
所以本模块只做两件事：
  ① 把**未终态任务**置 `cancelled` —— worker 在产出热路径上的探针会看到，自行 break；
  ② 把 checkpoint 置 `interrupted` —— 主循环在**派发边界**看到就停止派发。
⇒ 代价：正在飞行的那一次 LLM 调用会跑完（无法中断），但不会再派新任务。

## 为什么标志放在 DB 而不是进程内 Event
本仓是单进程 uvicorn（线程池），但**重启/多 worker/门禁夹具**都会让进程内 Event 失效；
而 DB 标志在所有这些场景下语义一致，且**中断本身就是要落库的事实**
（谁在什么时候叫停了哪一批、补了什么话）。轮询成本由调用方节流（默认 2 s）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("mbse.orchestration.interrupt")

__all__ = ["request_interrupt", "stop_requested", "latest_instruction",
           "consume_instruction", "interrupt_history", "active_run", "STOP_PHASES"]

#: 主循环/worker 见到这些 phase 就应当停止推进
#: · interrupted —— 用户中断（保留续跑资格）
#: · cancelled   —— 用户/巡检硬取消（不保留）
STOP_PHASES = ("interrupted", "cancelled")


def _ttl_note(run_id: int) -> str:
    return "批次 #%s" % run_id


def request_interrupt(conn, run_id: int, *, instruction: str = "", reason: str = "",
                      requested_by: str = "", kind: str = "interrupt") -> dict:
    """登记一次中断，并把该批次的未终态任务**协作式**置停。

    :returns: {"ok", "run_id", "phase", "cancelled_tasks", "instruction", "reason", "error"?}

    幂等：同一批次重复中断不会报错（第二次返回 cancelled_tasks=0），
    但**每次都会留一行台账**（"我又催了一次"本身是有信息量的）。
    """
    from agent.orch_checkpoint import load as _ck_load, touch as _ck_touch
    rid = int(run_id or 0)
    out = {"ok": False, "run_id": rid, "phase": "", "cancelled_tasks": 0,
           "instruction": (instruction or "")[:2000], "reason": (reason or "")[:500]}
    if not rid:
        out["error"] = "run_id 必填"
        return out
    ck = _ck_load(conn, rid)
    if not ck:
        out["error"] = "批次不存在（可能产生于检查点功能上线之前）"
        return out
    phase0 = str(ck.get("phase") or "")
    if phase0 in ("done", "failed"):
        out["error"] = "批次已结束（phase=%s），无需中断" % phase0
        out["phase"] = phase0
        return out
    conv_id = int(ck.get("conversation_id") or 0)

    # ① 协作式停止：把**未终态**任务置 cancelled（worker 探针会看到并自行收敛）
    n = 0
    try:
        n = int(conn.execute(
            "UPDATE agent_tasks SET status='cancelled', error=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE run_id=? "
            "AND status NOT IN ('done','failed','cancelled','canceled')",
            ("用户中断：%s" % (reason or "已请求停止")[:200], rid)).rowcount or 0)
        conn.commit()
    except Exception as e:
        logger.warning("中断时停止任务失败（不阻断台账写入）: %s", e)

    # ② checkpoint 置 interrupted —— 主循环据此停派发、resume 据此放行重排。
    #   ⚠️ 这里**不同于** `mark_interrupted`（孤儿回收，刻意不刷 updated_at）：
    #   用户主动中断是一次**有意义的进展**，刷 updated_at 让前端立刻看到"刚刚停下"，
    #   也让 `resumable()` 的心跳判据不会把它当"可能还在跑"。
    phase = "interrupted" if kind != "cancel" else "cancelled"
    try:
        _ck_touch(conn, rid, phase)
    except Exception as e:
        logger.warning("中断时写 checkpoint 失败: %s", e)

    # ③ 台账
    try:
        cur = conn.execute(
            "INSERT INTO orch_interrupts (run_id, conversation_id, kind, reason, instruction, "
            "requested_by, cancelled_tasks) VALUES (?,?,?,?,?,?,?)",
            (rid, conv_id, kind, (reason or "")[:500], (instruction or "")[:2000],
             (requested_by or "")[:100], n))
        conn.commit()
        out["interrupt_id"] = int(cur.lastrowid)
    except Exception as e:
        # 台账写不进也不能否认"已经停了" —— 但必须留痕
        logger.warning("中断台账写入失败: %s", e)
        out["ledger_error"] = str(e)[:160]

    out.update({"ok": True, "phase": phase, "cancelled_tasks": n})
    return out


def active_run(conn, conversation_id: int) -> dict:
    """该会话**当前在跑**的编排批次（最新一个非终态），没有则 {}。

    为什么放在本模块而不是 router 里：
      ① 收敛口径 —— 会话级中断端点与服务端自检都要"同一个判据"；
      ② `verify_layering.py` 的 L1c 明确盯 routers 里的直连 SQL（超出基线即红），
         而"哪一批还活着"本来就是编排领域的事，不该写在 HTTP 层。
    ⚠️ 只认 **planned/executing/summarizing** —— `interrupted`（暂停）与 `cancelled`（硬取消）
       都**不在跑**。首版把 `interrupted` 也算"在跑"（判据是 NOT IN done/failed/cancelled），
       实测后果：对一批早就暂停的会话再点"停止"会回 `ok:true`，
       用户以为"确实停住了"，其实那批早停了 —— 属"把无操作报成有操作"。
    """
    try:
        row = conn.execute(
            "SELECT run_id, phase, conversation_id FROM orch_checkpoints "
            "WHERE conversation_id=? AND phase IN ('planned','executing','summarizing') "
            "ORDER BY run_id DESC LIMIT 1", (int(conversation_id or 0),)).fetchone()
        if not row:
            return {}
        return dict(row) if hasattr(row, "keys") else {
            "run_id": row[0], "phase": row[1], "conversation_id": row[2]}
    except Exception:
        return {}


def _phase_of(conn, run_id: int) -> str:
    row = conn.execute("SELECT phase FROM orch_checkpoints WHERE run_id=?",
                       (int(run_id),)).fetchone()
    if not row:
        return ""
    return str(row[0] if not hasattr(row, "keys") else row["phase"] or "")


def stop_requested(conn, run_id: int) -> bool:
    """主循环/worker 侧的探针：该批次是否已被要求停止。

    ⚠️ **绝不抛**：它跑在热路径上（每轮派发前、每段等待中），
    抛异常会把"可中断"变成新的崩溃源。查不到 ⇒ False（继续跑）。

    ★ 2026-11-10（浏览器实测后的修正）：`conn=None` ⇒ **自己开独立连接**。
      原因与仓库既有纪律同源（`TaskQueue.is_cancel_requested` 的注释已写明）：
      编排主连接可能正持有**未提交的写事务**，在它上面查会与写锁纠缠
      （本轮实测 3 个并发 run 时出现 `database is locked`，服务无响应）。
      ⇒ 主循环调用时传 `None`，让它走独立短连接，读完即关。
    """
    rid = int(run_id or 0)
    if not rid:
        return False
    try:
        if conn is not None:
            return _phase_of(conn, rid) in STOP_PHASES
        from database import get_db
        c = get_db()
        try:
            return _phase_of(c, rid) in STOP_PHASES
        finally:
            c.close()
    except Exception:
        return False


def latest_instruction(conn, run_id: int) -> str:
    """该批次最近一次**带内容**的补充指令（空 = 没有）。

    ⚠️ 判据是「最新的**非空**指令」，不是「最新一行的指令」：
    用户可以中断两次（第二次只是催一句、没补新方向），
    若按"最新一行"取值，第二次的空串会把第一次明确说过的方向**抹掉** ——
    "没再说一遍"不等于"撤销"（与槽位治理里"空值不覆盖已有值"同一条纪律）。
    """
    try:
        row = conn.execute(
            "SELECT instruction FROM orch_interrupts WHERE run_id=? AND kind='interrupt' "
            "AND COALESCE(instruction,'')<>'' ORDER BY id DESC LIMIT 1",
            (int(run_id or 0),)).fetchone()
        if not row:
            return ""
        return str((row[0] if not hasattr(row, "keys") else row["instruction"]) or "")
    except Exception:
        return ""


def consume_instruction(conn, run_id: int) -> str:
    """取走补充指令并标记已消费（同一条指令只注入一次）。

    为什么必须标消费：resume 可以被人点多次；若不标，同一条指令会被反复
    追加进任务上下文 ⇒ 上下文里出现 N 份相同约束（本仓已有"同一段文本进 3 次"的前科）。
    """
    try:
        row = conn.execute(
            "SELECT id, instruction FROM orch_interrupts WHERE run_id=? "
            "AND kind='interrupt' AND COALESCE(consumed_at,'')='' "
            "AND COALESCE(instruction,'')<>'' "
            "ORDER BY id DESC LIMIT 1", (int(run_id or 0),)).fetchone()
        if not row:
            return ""
        _id = int(row[0] if not hasattr(row, "keys") else row["id"])
        txt = str((row[1] if not hasattr(row, "keys") else row["instruction"]) or "")
        conn.execute("UPDATE orch_interrupts SET consumed_at=CURRENT_TIMESTAMP WHERE id=?",
                     (_id,))
        conn.commit()
        return txt
    except Exception:
        return ""


def interrupt_history(conn, run_id: int) -> list:
    """某批次的中断台账（最新在前），供详情端点展示。"""
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, kind, reason, instruction, requested_by, cancelled_tasks, "
            "consumed_at, created_at FROM orch_interrupts WHERE run_id=? "
            "ORDER BY id DESC LIMIT 20", (int(run_id or 0),)).fetchall()]
    except Exception:
        return []
