# -*- coding: utf-8 -*-
"""P0-3（2026-10-28）计划外化的数据访问层 + `todo_write` 工具处理器。

方案：docs/持续自主执行能力-最终方案设计-20261028.md §4.1 / §6 第 3 步。

分层（与既有分工一致）：
  · `agent/agent_todo.py` —— 纯函数层（状态机 / 完成判定 / 障碍分类），零 IO
  · **本文件** —— IO 层（落库 / 读清单）+ 工具 handler

**所有状态迁移必须过 `agent_todo.transition()`**，不在本文件另写一套规则 ——
否则"两处各写一份规则"必然错位（这正是本仓反复踩过的坑）。
"""

from __future__ import annotations

import json
import time

__all__ = [
    "TODO_ENABLED_KEY",
    "todo_tool_def",
    "handle_todo_write",
    "list_todos",
    "load_todo_items",
    "todo_gate_for_conv",
    "upsert_todo",
]


def _cfg():
    from core import config as _c
    return _c


# ── 工具定义 ────────────────────────────────────────────────

_TODO_WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "todo_write",
        "description": (
            "管理本会话的**待办清单**（计划外化）。当任务是**多步**（≥3 步）时，"
            "先用它建清单，再逐步推进并结项。\n"
            "action 三选一：\n"
            "· create —— 建一条待办（subject 必填；verify 写**怎么验收**，如"
            "『调 sysmlv2_check 校验包 P-001』；不可自动验收的写『人工确认』）\n"
            "· update —— 更新状态。**置 completed 必须同时给 verify_result（验收回执）**"
            "（如『PASS: 0 errors』），否则会被拒绝；置 blocked 必须给 blocked_by 说明卡在哪\n"
            "· list —— 读当前清单（含每条的状态与验收回执）\n"
            "⚠️ 清单不是可选的：完成判定以**待办全部通过验收**为准，"
            "「建议下一步做 X」不算完成 —— 那就建一条 todo 并把它做完。\n"
            "⚠️ 同时至多一条 in_progress。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "list"],
                    "description": "操作类型：create 建待办 / update 改状态 / list 读清单",
                },
                "id": {
                    "type": "integer",
                    "description": "（update 必填）要更新的待办 id（list 的返回里有）",
                },
                "subject": {
                    "type": "string",
                    "description": "（create 必填）待办标题，具体到对象（如『校验包 P-001 的 SysML v2 语法』）",
                },
                "verify": {
                    "type": "string",
                    "description": "（create 必填）验收方式（可执行描述）。例：『调 sysmlv2_check 校验 P-001』；不可自动验收写『人工确认』",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "blocked", "deleted"],
                    "description": "（update 必填）目标状态",
                },
                "verify_result": {
                    "type": "string",
                    "description": "（update 置 completed 时必填）验收回执，表示验收已通过。例：『PASS: 0 errors』",
                },
                "blocked_by": {
                    "type": "string",
                    "description": "（update 置 blocked 时必填）卡在哪 / 依赖什么",
                },
                "note": {"type": "string", "description": "（可选）备注"},
            },
            "required": ["action"],
        },
    },
}


def todo_tool_def() -> dict:
    """返回 `todo_write` 的工具定义（供注入侧使用）。"""
    return _TODO_WRITE_TOOL


# ── 数据访问 ────────────────────────────────────────────────

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def list_todos(conn, conversation_id: int, include_deleted: bool = False) -> list:
    """读会话待办清单（按 seq、id 排序）。

    :returns: list[dict]，含 id/subject/status/verify/verify_result/blocked_by/note
    """
    if not conversation_id:
        return []
    sql = ("SELECT id, seq, subject, status, verify, verify_result, blocked_by, note "
           "FROM agent_todos WHERE conversation_id=?")
    params = [int(conversation_id)]
    if not include_deleted:
        sql += " AND status != 'deleted'"
    sql += " ORDER BY COALESCE(seq, id), id"
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        # ⚠️ sqlite3.Row 没有 .get() —— 用位置索引，别用 .get()
        out.append({
            "id": r[0], "seq": r[1], "subject": r[2], "status": r[3],
            "verify": r[4] or "", "verify_result": r[5] or "",
            "blocked_by": r[6] or "", "note": r[7] or "",
        })
    return out


def load_todo_items(conn, conversation_id: int, include_deleted: bool = False):
    """读清单并转成 `agent_todo.TodoItem`（供 `todo_gate()` 消费）。"""
    from agent import agent_todo as _at
    rows = list_todos(conn, conversation_id, include_deleted)
    return [_at.TodoItem(subject=r["subject"], status=r["status"],
                         verify=r["verify"], verify_result=r["verify_result"],
                         blocked_by=r["blocked_by"], note=r["note"])
            for r in rows]


def todo_gate_for_conv(conn, conversation_id: int) -> dict:
    """对本会话跑 `todo_gate()`；无 todo ⇒ 返回 fallback 形态（走旧路径）。

    ⚠️ 关键：**"有 todo 但清单全被删" ≠ "没有 todo"**。
       前者必须判为未完成（否则"删光待办就变成自动完成"成了后门）。
       ⇒ 用 `include_deleted=True` 统计"是否存在过 todo"，
         用 `include_deleted=False` 参与完成判定。
    """
    from agent import agent_todo as _at
    if not conversation_id:
        return {"done": False, "next_step": "", "remaining": [],
                "fallback": True, "reason": "无会话上下文 ⇒ 退回 LLM 评估器"}
    try:
        live = load_todo_items(conn, conversation_id, include_deleted=False)
        ever = load_todo_items(conn, conversation_id, include_deleted=True)
    except Exception:
        return {"done": False, "next_step": "", "remaining": [],
                "fallback": True, "reason": "读待办失败 ⇒ 退回 LLM 评估器"}
    if not ever:
        return _at.todo_gate(live, has_todos=False)
    return _at.todo_gate(live, has_todos=True)


def upsert_todo(conn, conversation_id: int, *, subject=None, verify=None,
                seq=None, note=None) -> dict:
    """建一条待办（create）。返回 {ok, id, result}。"""
    from agent import agent_todo as _at
    subject = str(subject or "").strip()
    if not subject:
        return {"ok": False, "result": "todo_write create 缺少 subject（待办标题）"}
    if not conversation_id:
        return {"ok": False, "result": "todo_write 无会话上下文，无法建待办"}
    verify = str(verify or "").strip()
    if not verify:
        return {"ok": False,
                "result": "todo_write create 缺少 verify（验收方式）——"
                          "每条待办必须写清楚『怎么算做完了』；"
                          "不可自动验收的写『人工确认』"}
    # seq：同会话内递增（取 max+1），保证"第一个 pending"有确定序
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM agent_todos WHERE conversation_id=?",
            (int(conversation_id),)).fetchone()
        auto_seq = int(row[0]) + 1 if row else 1
    except Exception:
        auto_seq = 1
    use_seq = int(seq) if seq not in (None, "") else auto_seq
    try:
        cur = conn.execute(
            "INSERT INTO agent_todos (conversation_id, run_id, seq, subject, status, "
            "verify, verify_result, blocked_by, note, created_at, updated_at) "
            "VALUES (?,?,?,?,'pending',?,'','',?,?,?)",
            (int(conversation_id), 0, use_seq, subject, verify,
             str(note or ""), _now(), _now()))
        conn.commit()
        return {"ok": True, "id": int(cur.lastrowid),
                "result": "已建待办 #%d「%s」（验收方式：%s）"
                          % (int(cur.lastrowid), subject[:60], verify[:60])}
    except Exception as e:
        return {"ok": False, "result": "建待办失败：%r" % (e,)}


# ── 工具 handler ────────────────────────────────────────────

def handle_todo_write(arguments: dict, conversation_id: int, conn=None) -> dict:
    """`todo_write` 工具 handler。返回 {ok, result, ...}。

    状态迁移**一律走** `agent_todo.transition()` —— 不在本文件另写规则。

    :param conn: 可选的数据库连接。**默认 None ⇒ 用生产 `db_conn()`**。
                 显式传入 ⇒ 用给定连接（门禁脚本用独立测试库，不碰生产库）。
                 ⚠️ 这一参数不是"为测试而设"：它让本函数**可被独立驱动**，
                   也避免"生产库尚未建表时工具直接报 no such table"这类
                   无法区分「表没建」与「参数错」的失败。
    """
    from agent import agent_todo as _at

    a = arguments or {}
    action = str(a.get("action") or "").strip().lower()
    if action not in ("create", "update", "list"):
        return {"ok": False, "result": "todo_write 的 action 必须是 create / update / list"}

    def _run(c):
        if action == "list":
            rows = list_todos(c, conversation_id)
            if not rows:
                return {"ok": True, "result": "当前会话没有待办清单（空）",
                        "todos": []}
            lines = []
            for r in rows:
                lines.append(
                    "#%d [%s] %s｜验收：%s｜回执：%s"
                    % (r["id"], r["status"], r["subject"],
                       r["verify"] or "（未填）", r["verify_result"] or "（无）"))
            return {"ok": True, "result": "当前待办清单：\n" + "\n".join(lines),
                    "todos": rows}

        if action == "create":
            return upsert_todo(c, conversation_id,
                               subject=a.get("subject"), verify=a.get("verify"),
                               seq=a.get("seq"), note=a.get("note"))

        # ── update ──
        tid = a.get("id")
        try:
            tid = int(tid)
        except Exception:
            return {"ok": False,
                    "result": "todo_write update 缺少有效的 id（先用 action=list 查）"}
        row = c.execute(
            "SELECT id, subject, status, verify, verify_result, blocked_by, note "
            "FROM agent_todos WHERE id=? AND conversation_id=?",
            (tid, int(conversation_id or 0))).fetchone()
        if not row:
            return {"ok": False,
                    "result": "todo_write update：待办 #%d 不存在（或不属于本会话）" % tid}
        item = _at.TodoItem(subject=row[1], status=row[2], verify=row[3],
                            verify_result=row[4], blocked_by=row[5], note=row[6])
        to_status = str(a.get("status") or "").strip().lower()
        if not to_status:
            return {"ok": False, "result": "todo_write update 缺少 status（目标状态）"}
        try:
            _at.transition(item, to_status,
                           verify_result=a.get("verify_result"),
                           blocked_by=a.get("blocked_by"),
                           note=a.get("note"))
        except _at.TransitionError as e:
            # 非法迁移**如实报错**（不静默接受）——模型要知道自己写错了
            return {"ok": False, "result": "todo_write update 被拒：%s" % e}
        try:
            c.execute(
                "UPDATE agent_todos SET status=?, verify=?, verify_result=?, "
                "blocked_by=?, note=?, updated_at=? WHERE id=? AND conversation_id=?",
                (item.status, item.verify, item.verify_result,
                 item.blocked_by, item.note, _now(), tid, int(conversation_id or 0)))
            c.commit()
        except Exception as e:
            return {"ok": False, "result": "更新待办失败：%r" % (e,)}
        return {"ok": True,
                "result": "待办 #%d「%s」→ %s%s"
                          % (tid, item.subject[:50], item.status,
                             ("（回执：%s）" % item.verify_result[:40])
                             if item.verify_result else "")}

    try:
        if conn is not None:
            return _run(conn)
        from database import db_conn
        with db_conn() as c:
            return _run(c)
    except Exception as e:
        return {"ok": False, "result": "todo_write 执行异常：%r" % (e,)}
